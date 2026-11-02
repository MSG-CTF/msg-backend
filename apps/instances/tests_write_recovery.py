import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    DatabaseError,
    IntegrityError,
    close_old_connections,
    connection,
    connections,
)
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.instances import tests as fixtures
from apps.instances.models import (
    DeleteReason,
    Instance,
    InstanceStatus,
    InstanceWriteOperation,
)
from apps.instances.services import SchedulerError
from apps.instances.write_recovery import recover_write


class Fixtures:
    setUp = fixtures.InstanceLockTests.setUp
    auth = fixtures.InstanceLockTests.auth

    def old_instance(self):
        return Instance.objects.create(
            user=self.user,
            team=self.team,
            challenge=self.challenge,
            release=self.release,
            status=InstanceStatus.RUNNING,
        )

    def post(self, source=None):
        if source:
            return self.client.post(
                f"/api/v1/instances/{source.pk}/reset", {}, format="json"
            )
        return self.client.post(
            "/api/v1/instances", {"challenge_id": str(self.challenge.pk)}, format="json"
        )

    def detail(self, row, **overrides):
        return {
            "instance_id": str(row.remote_instance_id),
            "user_id": str(self.user.pk),
            "team_id": str(self.team.pk),
            "challenge_id": str(self.challenge.pk),
            "release_id": str(self.release.pk),
            "registry_revision": self.release.registry_revision,
            "status": "RUNNING",
            **overrides,
        }

    def failed_write(self, source=None):
        remote_id = uuid.uuid4()
        target = "call_scheduler_reset" if source else "call_scheduler_create"
        with patch(
            f"apps.instances.views.{target}",
            return_value={
                "instance_id": str(remote_id),
                "status": "REQUESTED",
            },
        ) as remote, patch(
            "apps.instances.services.Instance.objects.update_or_create",
            side_effect=IntegrityError("local injected write failure"),
        ):
            self.assertEqual(self.post(source).status_code, 500)
        remote.assert_called_once()
        row = InstanceWriteOperation.objects.get(remote_instance_id=remote_id)
        self.assertEqual(row.status, "RECOVERY_PENDING")
        self.assertEqual(row.last_error_code, "DB_WRITE_FAILED")
        self.assertEqual(row.user_id, self.user.pk)
        self.assertEqual(row.team_id, self.team.pk)
        self.assertEqual(row.release_id, self.release.pk)
        self.assertFalse(Instance.objects.filter(pk=remote_id).exists())
        return row


@override_settings(
    SECURE_SSL_REDIRECT=False, SCHEDULER_API_TOKEN="local-recovery-token"
)
class InstanceWriteRecoveryTests(Fixtures, TestCase):
    def test_create_failure_keeps_remote_id_and_blocks_repeated_create(self):
        row = self.failed_write()
        self.assertEqual(row.action, "CREATE")
        with patch("apps.instances.views.call_scheduler_create") as create:
            response = self.post()
        # 기다려도 풀리지 않는 대기라 서버 장애(503)와 다른 코드로 운영진 확인을 안내한다
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INSTANCE_REVIEW_REQUIRED")
        self.assertIn("운영진에게 알려주세요", response.json()["message"])
        create.assert_not_called()
        self.assertEqual(InstanceWriteOperation.objects.count(), 1)

    def test_reset_failure_blocks_participant_and_admin_reset(self):
        source = self.old_instance()
        row = self.failed_write(source)
        self.assertEqual(row.source_instance_id, source.pk)
        with patch("apps.instances.views.call_scheduler_reset") as reset:
            self.assertEqual(self.post(source).status_code, 409)
        reset.assert_not_called()
        source.refresh_from_db()
        self.assertEqual(source.status, InstanceStatus.RUNNING)
        admin = User.objects.create_user(login_id="recovery-admin", role=Role.ADMIN)
        self.client.force_authenticate(admin)
        with patch("apps.adminpanel.views.call_scheduler_reset") as reset:
            response = self.client.post(
                f"/api/v1/admin/instances/{source.pk}/reset", {}, format="json"
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "INSTANCE_REVIEW_REQUIRED")
        self.assertIn("recover_instance_writes --show", response.json()["message"])
        reset.assert_not_called()

    def test_recovery_adopts_exact_remote_and_original_release_once(self):
        source = self.old_instance()
        row = self.failed_write(source)
        with patch(
            "apps.instances.write_recovery.call_scheduler_detail",
            return_value=self.detail(row),
        ) as detail, patch("apps.instances.views.call_scheduler_reset") as reset:
            self.assertEqual(recover_write(row.pk), "COMPLETED")
            self.assertEqual(recover_write(row.pk), "COMPLETED")
        reset.assert_not_called()
        detail.assert_called_once()
        self.assertEqual(detail.call_args.args[0].pk, row.remote_instance_id)
        self.assertEqual(detail.call_args.args[1], "Bearer local-recovery-token")
        restored = Instance.objects.get(pk=row.remote_instance_id)
        self.assertEqual(restored.release_id, self.release.pk)
        self.assertEqual(restored.replaced_instance_id, source.pk)
        source.refresh_from_db()
        self.assertEqual(source.status, InstanceStatus.STOPPING)
        self.assertEqual(source.delete_reason, DeleteReason.REPLACED_BY_NEW_INSTANCE)
        row.refresh_from_db()
        self.assertEqual(row.attempts, 1)

    def test_existing_recovered_instance_is_not_overwritten_with_stale_state(self):
        row = self.failed_write()
        existing = Instance.objects.create(
            instance_id=row.remote_instance_id,
            user=self.user,
            team=self.team,
            challenge=self.challenge,
            release=self.release,
            status="CLEANED",
            extend_count=2,
        )
        with patch("apps.instances.write_recovery.call_scheduler_detail") as detail:
            self.assertEqual(recover_write(row.pk), "COMPLETED")
        detail.assert_not_called()
        existing.refresh_from_db()
        self.assertEqual((existing.status, existing.extend_count), ("CLEANED", 2))

    def test_remote_error_does_not_recreate_or_claim_cleanup(self):
        row = self.failed_write()
        for status, expected in ((503, "RECOVERY_PENDING"), (404, "REVIEW_REQUIRED")):
            with self.subTest(status=status), patch(
                "apps.instances.write_recovery.call_scheduler_detail",
                side_effect=SchedulerError(
                    "INSTANCE_NOT_FOUND", "not confirmed", status
                ),
            ):
                self.assertEqual(recover_write(row.pk), expected)
        self.assertFalse(Instance.objects.exists())

    def test_recovery_rejects_scope_mismatch(self):
        row = self.failed_write()
        for field in (
            "instance_id",
            "user_id",
            "team_id",
            "challenge_id",
            "release_id",
        ):
            with self.subTest(field=field):
                InstanceWriteOperation.objects.filter(pk=row.pk).update(
                    status="RECOVERY_PENDING"
                )
                with patch(
                    "apps.instances.write_recovery.call_scheduler_detail",
                    return_value=self.detail(row, **{field: str(uuid.uuid4())}),
                ):
                    self.assertEqual(recover_write(row.pk), "REVIEW_REQUIRED")
                self.assertFalse(Instance.objects.exists())

    def test_db_failure_during_recovery_is_retryable(self):
        row = self.failed_write()
        with patch(
            "apps.instances.write_recovery.call_scheduler_detail",
            return_value=self.detail(row),
        ), patch(
            "apps.instances.services.Instance.objects.update_or_create",
            side_effect=IntegrityError("injected recovery failure"),
        ):
            self.assertEqual(recover_write(row.pk), "RECOVERY_PENDING")
        row.refresh_from_db()
        self.assertEqual((row.attempts, row.last_error_code), (1, "DB_WRITE_FAILED"))
        with patch(
            "apps.instances.write_recovery.call_scheduler_detail",
            return_value=self.detail(row),
        ):
            self.assertEqual(recover_write(row.pk), "COMPLETED")
        row.refresh_from_db()
        self.assertEqual(row.attempts, 2)

    def test_owner_change_or_deletion_preserves_record_for_review(self):
        row = self.failed_write()
        other = Team.objects.create(team_name="moved recovery owner")
        User.objects.filter(pk=self.user.pk).update(team=other)
        with patch("apps.instances.write_recovery.call_scheduler_detail") as detail:
            self.assertEqual(recover_write(row.pk), "REVIEW_REQUIRED")
        detail.assert_not_called()
        self.team.delete()
        self.assertTrue(InstanceWriteOperation.objects.filter(pk=row.pk).exists())

    def test_rejected_request_can_be_retried_but_ambiguous_call_requires_review(self):
        with patch(
            "apps.instances.views.call_scheduler_create",
            side_effect=SchedulerError("INSTANCE_REPLACEMENT_REQUIRED", "confirm", 409),
        ):
            self.assertEqual(self.post().status_code, 409)
        self.assertEqual(InstanceWriteOperation.objects.get().status, "REJECTED")
        with patch(
            "apps.instances.views.call_scheduler_create",
            side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "unknown", 503),
        ):
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(
            InstanceWriteOperation.objects.filter(status="REVIEW_REQUIRED").count(), 1
        )
        with patch("apps.instances.views.call_scheduler_create") as create:
            self.assertEqual(self.post().status_code, 409)
        create.assert_not_called()

    def test_interrupted_started_operation_is_not_automatically_reissued(self):
        row = InstanceWriteOperation.objects.create(
            action="CREATE",
            user_id=self.user.pk,
            team_id=self.team.pk,
            challenge_id=self.challenge.pk,
            release_id=self.release.pk,
        )
        self.assertEqual(recover_write(row.pk), "STARTED")
        with patch("apps.instances.views.call_scheduler_create") as create:
            self.assertEqual(self.post().status_code, 409)
        create.assert_not_called()

    def test_command_is_read_only_with_show_and_processes_selected_record(self):
        row = self.failed_write()
        output = io.StringIO()
        with patch("apps.instances.write_recovery.call_scheduler_detail") as detail:
            call_command("recover_instance_writes", show=True, stdout=output)
        detail.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["operation_id"], str(row.pk))
        self.assertNotIn("local-recovery-token", output.getvalue())
        with patch(
            "apps.instances.write_recovery.call_scheduler_detail",
            return_value=self.detail(row),
        ):
            call_command(
                "recover_instance_writes", operation_id=row.pk, stdout=io.StringIO()
            )
        row.refresh_from_db()
        self.assertEqual(row.status, "COMPLETED")
        with self.assertRaises(CommandError):
            call_command("recover_instance_writes", limit=0)

    def test_failed_remote_cleanup_state_and_reason_are_preserved(self):
        row = self.failed_write()
        with patch(
            "apps.instances.write_recovery.call_scheduler_detail",
            return_value=self.detail(
                row,
                status="CLEANED",
                delete_reason="CREATE_FAILED_CLEANUP",
            ),
        ):
            self.assertEqual(recover_write(row.pk), "COMPLETED")
        restored = Instance.objects.get(pk=row.remote_instance_id)
        self.assertEqual(restored.status, "CLEANED")
        self.assertEqual(restored.delete_reason, DeleteReason.CREATE_FAILED_CLEANUP)

    def test_unknown_release_or_malformed_result_is_not_automatically_retried(self):
        row = self.failed_write()
        for extra in ({"registry_revision": 99999}, {"endpoints": "malformed"}):
            InstanceWriteOperation.objects.filter(pk=row.pk).update(
                status="RECOVERY_PENDING"
            )
            with self.subTest(extra=extra), patch(
                "apps.instances.write_recovery.call_scheduler_detail",
                return_value=self.detail(row, **extra),
            ):
                self.assertEqual(recover_write(row.pk), "REVIEW_REQUIRED")
            self.assertFalse(Instance.objects.exists())

    def test_source_save_failure_preserves_new_remote_id_for_recovery(self):
        source = self.old_instance()
        remote_id = uuid.uuid4()
        with patch(
            "apps.instances.views.call_scheduler_reset",
            return_value={"instance_id": str(remote_id), "status": "REQUESTED"},
        ), patch(
            "apps.instances.views.mark_instance_replaced",
            side_effect=DatabaseError("injected source save failure"),
        ):
            self.assertEqual(self.post(source).status_code, 500)
        row = InstanceWriteOperation.objects.get()
        self.assertEqual(
            (row.remote_instance_id, row.status), (remote_id, "RECOVERY_PENDING")
        )
        self.assertFalse(Instance.objects.filter(pk=remote_id).exists())

    def test_apply_uses_journal_release_even_after_admin_activation(self):
        from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig

        row = self.failed_write()
        other = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=2,
            registry_revision=2,
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
        )
        ChallengeRuntimeConfig.objects.filter(challenge=self.challenge).update(
            current_release=other
        )
        with patch(
            "apps.instances.write_recovery.call_scheduler_detail",
            return_value=self.detail(row),
        ):
            self.assertEqual(recover_write(row.pk), "COMPLETED")
        self.assertEqual(
            Instance.objects.get(pk=row.remote_instance_id).release_id, self.release.pk
        )

    def test_membership_change_between_journal_and_remote_call_is_rejected(self):
        from apps.instances.write_recovery import begin_write

        other = Team.objects.create(team_name="new owner team")

        def move_owner(**kwargs):
            row = begin_write(**kwargs)
            User.objects.filter(pk=self.user.pk).update(team=other)
            return row

        with patch("apps.instances.views.begin_write", side_effect=move_owner), patch(
            "apps.instances.views.call_scheduler_create"
        ) as create:
            self.assertEqual(self.post().status_code, 403)
        create.assert_not_called()
        self.assertEqual(InstanceWriteOperation.objects.get().status, "REJECTED")

    def test_explicit_command_rechecks_review_without_bypassing_scope(self):
        row = self.failed_write()
        InstanceWriteOperation.objects.filter(pk=row.pk).update(
            status="REVIEW_REQUIRED"
        )
        with patch(
            "apps.instances.write_recovery.call_scheduler_detail",
            return_value=self.detail(row),
        ) as detail:
            self.assertEqual(recover_write(row.pk), "REVIEW_REQUIRED")
            detail.assert_not_called()
            call_command(
                "recover_instance_writes", operation_id=row.pk, stdout=io.StringIO()
            )
        row.refresh_from_db()
        self.assertEqual(row.status, "COMPLETED")

    def test_default_batch_does_not_starve_pending_work_behind_manual_review(self):
        manual = InstanceWriteOperation.objects.create(
            action="CREATE",
            user_id=uuid.uuid4(),
            team_id=self.team.pk,
            challenge_id=self.challenge.pk,
            status="REVIEW_REQUIRED",
        )
        row = self.failed_write()
        with patch(
            "apps.instances.write_recovery.call_scheduler_detail",
            return_value=self.detail(row),
        ):
            call_command("recover_instance_writes", limit=1, stdout=io.StringIO())
        row.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(row.status, "COMPLETED")
        self.assertEqual(manual.status, "REVIEW_REQUIRED")

    def test_create_and_reset_are_not_wrapped_in_request_wide_transactions(self):
        from django.urls import resolve

        for url in ("/api/v1/instances", f"/api/v1/instances/{uuid.uuid4()}/reset"):
            self.assertIn("default", resolve(url).func._non_atomic_requests)


@override_settings(
    SECURE_SSL_REDIRECT=False, SCHEDULER_API_TOKEN="local-recovery-token"
)
class InstanceWriteCommitTests(Fixtures, TransactionTestCase):
    def test_failed_journal_commit_does_not_call_scheduler(self):
        with patch.object(
            connection, "commit", side_effect=DatabaseError("journal unavailable")
        ), patch("apps.instances.views.call_scheduler_create") as create:
            self.assertEqual(self.post().status_code, 500)
        create.assert_not_called()
        self.assertFalse(InstanceWriteOperation.objects.exists())

    def test_journal_is_committed_before_remote_and_survives_result_commit_failure(
        self,
    ):
        remote_id = uuid.uuid4()
        commit = connection.commit
        calls = 0

        def commit_or_fail():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DatabaseError("injected result commit failure")
            return commit()

        with patch(
            "apps.instances.views.call_scheduler_create",
            return_value={"instance_id": str(remote_id), "status": "REQUESTED"},
        ) as create, patch.object(
            connection,
            "commit",
            side_effect=commit_or_fail,
        ):
            self.assertEqual(self.post().status_code, 500)
        create.assert_called_once()
        row = InstanceWriteOperation.objects.get()
        self.assertEqual(
            (row.status, row.remote_instance_id), ("RECOVERY_PENDING", remote_id)
        )
        self.assertFalse(Instance.objects.filter(pk=remote_id).exists())

    def test_competing_retry_does_not_send_another_remote_request(self):
        entered, proceed = Event(), Event()
        remote_id = uuid.uuid4()

        def create(*args, **kwargs):
            entered.set()
            if not proceed.wait(10):
                raise AssertionError("remote call was not released")
            return {"instance_id": str(remote_id), "status": "REQUESTED"}

        def post():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=self.user.pk))
                return client.post(
                    "/api/v1/instances",
                    {"challenge_id": str(self.challenge.pk)},
                    format="json",
                ).status_code
            finally:
                connections.close_all()

        with patch(
            "apps.instances.views.call_scheduler_create", side_effect=create
        ) as remote, patch(
            "apps.instances.services.Instance.objects.update_or_create",
            side_effect=IntegrityError("injected save failure"),
        ), ThreadPoolExecutor(
            max_workers=2
        ) as pool:
            first = pool.submit(post)
            try:
                self.assertTrue(entered.wait(10))
                second = pool.submit(post)
            finally:
                proceed.set()
            self.assertEqual(sorted([first.result(15), second.result(15)]), [409, 500])
        remote.assert_called_once()
        self.assertEqual(InstanceWriteOperation.objects.count(), 1)

    def test_two_recovery_workers_only_apply_once(self):
        row = self.failed_write()
        entered, proceed = Event(), Event()

        def detail(*args):
            entered.set()
            if not proceed.wait(10):
                raise AssertionError("recovery was not released")
            return self.detail(row)

        def recover():
            close_old_connections()
            try:
                return recover_write(row.pk)
            finally:
                connections.close_all()

        with patch(
            "apps.instances.write_recovery.call_scheduler_detail", side_effect=detail
        ) as remote, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(recover)
            try:
                self.assertTrue(entered.wait(10))
                second = pool.submit(recover)
            finally:
                proceed.set()
            self.assertEqual(
                [first.result(15), second.result(15)], ["COMPLETED", "COMPLETED"]
            )
        remote.assert_called_once()
        row.refresh_from_db()
        self.assertEqual(row.attempts, 1)
