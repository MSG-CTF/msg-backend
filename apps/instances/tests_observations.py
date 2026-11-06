import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.instances.models import ChallengeRelease, Instance
from apps.instances.observations import (
    record_scheduler_observation,
    serialize_scheduler_observation,
)
from apps.instances.reconciliation import reconcile_instances_once
from apps.instances.services import SchedulerError, SchedulerResponseError
from apps.instances.tests_reconciliation import Fixtures, LOCMEM


@override_settings(
    CACHES=LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="observation-test-token",
)
class SchedulerObservationTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            login_id="observation-admin", role=Role.ADMIN
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def listed(self, instance):
        response = self.client.get("/api/v1/admin/instances?summary=false")
        self.assertEqual(response.status_code, 200, response.data)
        return next(
            row
            for row in response.data["data"]["instances"]
            if row["instance_id"] == str(instance.pk)
        )

    def test_old_backend_inserts_without_observation_columns_still_work(self):
        instance = self.instance()
        new_id = uuid.uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO instances (
                    instance_id, user_id, team_id, challenge_id, status, host,
                    ports, endpoints, expires_at, hard_expires_at, extend_count,
                    replaced_instance_id, release_id, delete_reason,
                    created_at, updated_at
                )
                SELECT %s, user_id, team_id, challenge_id, status, host,
                    ports, endpoints, expires_at, hard_expires_at, extend_count,
                    replaced_instance_id, release_id, delete_reason,
                    created_at, updated_at
                FROM instances WHERE instance_id = %s
                """,
                [new_id, instance.pk],
            )
        legacy = Instance.objects.get(pk=new_id)
        self.assertIsNone(legacy.scheduler_status)
        self.assertIsNone(legacy.scheduler_checked_at)
        self.assertIsNone(legacy.scheduler_error_code)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_new_rows_are_unchecked_and_list_does_not_call_scheduler(self, detail):
        instance = self.instance()
        self.assertEqual(
            self.listed(instance)["scheduler_observation"],
            {
                "status": None,
                "checked_at": None,
                "check_status": "NOT_CHECKED",
                "error_code": None,
                "status_mismatch": None,
            },
        )
        detail.assert_not_called()

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_active_mismatch_is_observed_without_changing_runtime_fields(self, detail):
        instance = self.instance(status="PROVISIONING", delete_reason=None)
        before = self.lifecycle_values(instance)
        detail.return_value = {
            **self.snapshot(instance, "RUNNING"),
            "env": {"TOKEN": "private-env"},
            "host": "private-runtime-url",
        }
        self.assertEqual(reconcile_instances_once()["pending"], 1)
        self.assertEqual(self.lifecycle_values(instance), before)
        row = self.listed(instance)
        self.assertEqual(row["status"], "PROVISIONING")
        result = row["scheduler_observation"]
        self.assertEqual(
            (result["status"], result["check_status"], result["status_mismatch"]),
            ("RUNNING", "OK", True),
        )
        self.assertTrue(result["checked_at"].endswith("Z"))
        self.assertIsNone(result["error_code"])
        self.assertNotIn("private-", str(result))

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_matching_observation_does_not_reset_lifecycle_age(self, detail):
        instance = self.instance(status="RUNNING", delete_reason=None)
        before = instance.updated_at
        detail.return_value = self.snapshot(instance, "RUNNING")
        reconcile_instances_once()
        first = self.listed(instance)["scheduler_observation"]
        reconcile_instances_once()
        instance.refresh_from_db()
        self.assertEqual(instance.updated_at, before)
        result = self.listed(instance)["scheduler_observation"]
        self.assertEqual(
            (result["check_status"], result["status_mismatch"]), ("OK", False)
        )
        self.assertGreaterEqual(result["checked_at"], first["checked_at"])

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_successful_terminal_sync_is_a_fresh_match(self, detail):
        instance = self.instance()
        detail.return_value = self.snapshot(instance)
        self.assertEqual(reconcile_instances_once()["updated"], 1)
        result = self.listed(instance)
        self.assertEqual(result["status"], "CLEANED")
        self.assertEqual(result["scheduler_observation"]["check_status"], "OK")
        self.assertIs(result["scheduler_observation"]["status_mismatch"], False)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_lookup_failure_is_not_a_match_or_cleanup_evidence(self, detail):
        for status, expected, counter in (
            (401, "UNAUTHORIZED", "error"),
            (403, "FORBIDDEN", "error"),
            (404, "NOT_FOUND", "missing"),
            (502, "UNAVAILABLE", "error"),
            (503, "UNAVAILABLE", "error"),
        ):
            with self.subTest(status=status):
                Instance.objects.all().delete()
                cache.clear()
                instance = self.instance(status="FAILED")
                before = self.lifecycle_values(instance)
                detail.side_effect = SchedulerError(
                    "INSTANCE_NOT_FOUND" if status == 404 else "private-code",
                    "private-token-and-response",
                    status,
                )
                self.assertEqual(reconcile_instances_once()[counter], 1)
                self.assertEqual(self.lifecycle_values(instance), before)
                result = self.listed(instance)["scheduler_observation"]
                self.assertEqual(
                    (result["check_status"], result["error_code"]), ("ERROR", expected)
                )
                self.assertIsNone(result["status"])
                self.assertIsNone(result["status_mismatch"])
                self.assertIsNotNone(result["checked_at"])
                self.assertNotIn("private-", str(result))

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_rejected_identity_status_and_reason_never_become_valid_observations(
        self, detail
    ):
        instance = self.instance()
        release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=1,
            registry_revision=1,
            cpu_millicores=100,
            memory_mib=128,
            ephemeral_storage_mib=128,
        )
        Instance.objects.filter(pk=instance.pk).update(release=release)
        instance.refresh_from_db()
        before = self.lifecycle_values(instance)
        invalid = [
            {**self.snapshot(instance), field: str(uuid.uuid4())}
            for field in (
                "instance_id",
                "team_id",
                "challenge_id",
                "user_id",
                "release_id",
            )
        ]
        invalid.extend(
            [
                {**self.snapshot(instance), "status": "private-status"},
                {**self.snapshot(instance), "status": "RUNNING"},
                {**self.snapshot(instance), "delete_reason": "private-reason"},
            ]
        )
        invalid.extend(
            {
                key: value
                for key, value in self.snapshot(instance).items()
                if key != field
            }
            for field in (
                "instance_id",
                "team_id",
                "challenge_id",
                "release_id",
                "status",
            )
        )
        for body in invalid:
            with self.subTest(body=body):
                detail.return_value = body
                self.assertEqual(reconcile_instances_once()["error"], 1)
                self.assertEqual(self.lifecycle_values(instance), before)
                result = self.listed(instance)["scheduler_observation"]
                self.assertEqual(
                    (result["check_status"], result["error_code"]),
                    ("ERROR", "INVALID_RESPONSE"),
                )
                self.assertIsNone(result["status"])
                self.assertIsNone(result["status_mismatch"])
                self.assertNotIn("private-", str(result))

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_malformed_response_is_classified_without_raw_body(self, detail):
        instance = self.instance()
        for failure in (
            SchedulerResponseError("private-code", "private-response", 503),
            ValueError("private-response"),
        ):
            detail.side_effect = failure
            self.assertEqual(reconcile_instances_once()["error"], 1)
            result = self.listed(instance)["scheduler_observation"]
            self.assertEqual(result["error_code"], "INVALID_RESPONSE")
            self.assertNotIn("private-", str(result))

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_failure_clears_old_success_and_later_success_clears_error(self, detail):
        instance = self.instance(status="RUNNING", delete_reason=None)
        detail.return_value = self.snapshot(instance, "RUNNING")
        reconcile_instances_once()
        self.assertEqual(
            self.listed(instance)["scheduler_observation"]["status"], "RUNNING"
        )
        detail.side_effect = SchedulerError("SCHEDULER_UNAVAILABLE", "unavailable", 503)
        reconcile_instances_once()
        failed = self.listed(instance)["scheduler_observation"]
        self.assertEqual(failed["check_status"], "ERROR")
        self.assertIsNone(failed["status"])
        detail.side_effect = None
        reconcile_instances_once()
        result = self.listed(instance)["scheduler_observation"]
        self.assertEqual(result["check_status"], "OK")
        self.assertIsNone(result["error_code"])

    def test_old_future_and_locally_changed_observations_are_stale(self):
        instance = self.instance(status="RUNNING", delete_reason=None)
        now = timezone.now()
        for checked_at, updated_at in (
            (now - timedelta(seconds=61), now - timedelta(seconds=62)),
            (now + timedelta(seconds=1), instance.updated_at),
            (now - timedelta(seconds=2), now - timedelta(seconds=1)),
        ):
            with self.subTest(checked_at=checked_at, updated_at=updated_at):
                instance.scheduler_status = "FAILED"
                instance.scheduler_checked_at = checked_at
                instance.updated_at = updated_at
                result = serialize_scheduler_observation(instance, now)
                self.assertEqual(result["check_status"], "STALE")
                self.assertIsNone(result["status_mismatch"])

    def test_exact_freshness_boundary(self):
        instance = self.instance(status="RUNNING", delete_reason=None)
        now = timezone.now()
        instance.scheduler_status = "RUNNING"
        instance.scheduler_checked_at = now - timedelta(seconds=60)
        instance.updated_at = now - timedelta(seconds=61)
        self.assertEqual(
            serialize_scheduler_observation(instance, now)["check_status"], "OK"
        )

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_new_local_state_after_observation_is_not_reported_as_mismatch(
        self, detail
    ):
        instance = self.instance(status="RUNNING", delete_reason=None)
        detail.return_value = self.snapshot(instance, "RUNNING")
        reconcile_instances_once()
        Instance.objects.filter(pk=instance.pk).update(
            status="STOPPING", updated_at=timezone.now()
        )
        result = self.listed(instance)["scheduler_observation"]
        self.assertEqual(result["check_status"], "STALE")
        self.assertIsNone(result["status_mismatch"])

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_late_response_does_not_overwrite_changed_or_deleted_row(self, detail):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                instance = self.instance()

                def response(snapshot, auth):
                    if deleted:
                        Instance.objects.filter(pk=snapshot.pk).delete()
                    else:
                        Instance.objects.filter(pk=snapshot.pk).update(
                            status="CLEANED", updated_at=timezone.now()
                        )
                    return self.snapshot(snapshot)

                detail.side_effect = response
                self.assertEqual(
                    reconcile_instances_once(instance_ids=[instance.pk])["skipped"], 1
                )
                if deleted:
                    self.assertFalse(Instance.objects.filter(pk=instance.pk).exists())
                else:
                    instance.refresh_from_db()
                    self.assertIsNone(instance.scheduler_checked_at)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_late_error_does_not_overwrite_new_state(self, detail):
        instance = self.instance()

        def fail(snapshot, auth):
            Instance.objects.filter(pk=snapshot.pk).update(
                status="CLEANED", updated_at=timezone.now()
            )
            raise SchedulerError("INSTANCE_NOT_FOUND", "not found", 404)

        detail.side_effect = fail
        reconcile_instances_once()
        instance.refresh_from_db()
        self.assertEqual(instance.status, "CLEANED")
        self.assertIsNone(instance.scheduler_checked_at)

    def test_changed_scope_cannot_receive_an_observation(self):
        instance = self.instance()
        other = Team.objects.create(team_name="observation-other")
        Instance.objects.filter(pk=instance.pk).update(team=other)
        self.assertEqual(record_scheduler_observation(instance, status="CLEANED"), 0)
        instance.refresh_from_db()
        self.assertIsNone(instance.scheduler_checked_at)

    def test_observation_writer_only_accepts_safe_status_and_error_codes(self):
        instance = self.instance()
        for arguments in (
            {},
            {"status": "private-value"},
            {"error_code": "private-token"},
            {"status": "RUNNING", "error_code": "UNAVAILABLE"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                record_scheduler_observation(instance, **arguments)

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_final_rows_are_only_observed_when_explicitly_selected(self, detail):
        instance = self.instance(status="CLEANED")
        detail.return_value = self.snapshot(instance)
        self.assertEqual(reconcile_instances_once()["checked"], 0)
        self.assertEqual(
            self.listed(instance)["scheduler_observation"]["check_status"],
            "NOT_CHECKED",
        )
        self.assertEqual(
            reconcile_instances_once(instance_ids=[instance.pk])["checked"], 1
        )
        result = self.listed(instance)["scheduler_observation"]
        self.assertEqual(
            (result["check_status"], result["status_mismatch"]), ("OK", False)
        )
        detail.assert_called_once()

    @patch("apps.instances.services.urlopen")
    def test_list_does_not_add_per_row_queries_or_remote_calls(self, remote):
        self.instance()
        with CaptureQueriesContext(connection) as one:
            self.assertEqual(
                self.client.get("/api/v1/admin/instances?summary=false").status_code,
                200,
            )
        for _ in range(15):
            self.instance()
        with CaptureQueriesContext(connection) as many:
            response = self.client.get("/api/v1/admin/instances?summary=false")
        self.assertEqual(len(response.data["data"]["instances"]), 16)
        self.assertEqual(len(one), len(many))
        remote.assert_not_called()

    @patch("apps.instances.reconciliation.call_scheduler_detail")
    def test_existing_pagination_and_filters_still_apply(self, detail):
        self.instance(status="RUNNING")
        self.instance(status="FAILED")
        self.instance(status="RUNNING")
        response = self.client.get(
            "/api/v1/admin/instances",
            {
                "summary": "false",
                "status": "RUNNING",
                "team_id": str(self.team.pk),
                "challenge_id": str(self.challenge.pk),
                "page": 2,
                "size": 1,
            },
        )
        data = response.data["data"]
        self.assertEqual(
            (data["total_count"], data["page"], len(data["instances"])), (2, 2, 1)
        )
        self.assertIn("scheduler_observation", data["instances"][0])
        detail.assert_not_called()

    def test_observations_remain_admin_only(self):
        self.instance()
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get("/api/v1/admin/instances").status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/v1/admin/instances").status_code, 401)
