import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from django.db import IntegrityError, close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.adminpanel import tests as fixtures, views
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    Instance,
    InstanceLock,
    InstanceStatus,
    InstanceWriteOperation,
)
from apps.instances.services import SchedulerError
from apps.challenge.models import Challenge
from apps.board.models import Cell, TeamChallengeAccess


class Fixtures:
    auth = fixtures.AdminTeamManageTests.auth
    url = fixtures.AdminTeamManageTests.url
    patch = fixtures.AdminTeamManageTests.patch
    delete = fixtures.AdminTeamManageTests.delete
    events = fixtures.AdminTeamManageTests.events

    def setUp(self):
        fixtures.AdminTeamManageTests.setUp(self)
        self.challenge = Challenge.objects.create(
            title="cleanup",
            category="WEB",
            difficulty="EASY",
            score=100,
            flag_hash="unused",
            is_published=True,
        )

    def instance(self, status="FAILED", **fields):
        return Instance.objects.create(
            user=self.member,
            team=self.team,
            challenge=self.challenge,
            status=status,
            **fields,
        )

    def detail(self, instance, **fields):
        return {
            "instance_id": str(instance.pk),
            "team_id": str(instance.team_id),
            "challenge_id": str(instance.challenge_id),
            "user_id": str(instance.user_id),
            "release_id": str(instance.release_id) if instance.release_id else None,
            "status": instance.status,
            "runtime_target_id": None,
            **fields,
        }

    def mutate(self, mode):
        if mode == "delete":
            return self.delete({"reason": "cleanup QA"})
        return self.patch(
            {"remove_user_ids": [str(self.member.pk)], "reason": "cleanup QA"}
        )

    def assert_blocked(self, response, code, instance=None):
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], code)
        self.member.refresh_from_db()
        self.assertEqual(self.member.team_id, self.team.pk)
        self.assertEqual(self.events("TEAM_DELETED"), [])
        self.assertEqual(self.events("TEAM_UPDATED"), [])
        if instance:
            self.assertTrue(Instance.objects.filter(pk=instance.pk).exists())

    def operation(self, status, **fields):
        return InstanceWriteOperation.objects.create(
            action="CREATE",
            status=status,
            user_id=fields.pop("user_id", self.member.pk),
            team_id=fields.pop("team_id", self.team.pk),
            challenge_id=self.challenge.pk,
            **fields,
        )


@override_settings(
    CACHES=fixtures.LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="cleanup-service-token",
)
class TeamCleanupTests(Fixtures, TestCase):
    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_cleaned_instances_allow_mutation_without_scheduler(self, detail):
        for mode in ("delete", "remove"):
            with self.subTest(mode=mode), transaction.atomic():
                self.instance("CLEANED")
                self.assertEqual(self.mutate(mode).status_code, 200)
                transaction.set_rollback(True)
        detail.assert_not_called()

    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_every_other_state_blocks_mutation(self, detail):
        for status in (*InstanceStatus.values, "UNKNOWN"):
            if status in ("FAILED", "CLEANED"):
                continue
            for mode in ("delete", "remove"):
                with self.subTest(status=status, mode=mode), transaction.atomic():
                    instance = self.instance(status)
                    self.assert_blocked(
                        self.mutate(mode), "ACTIVE_INSTANCE_EXISTS", instance
                    )
                    transaction.set_rollback(True)
        detail.assert_not_called()

    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_failed_without_deployment_allows_mutation(self, detail):
        for mode in ("delete", "remove"):
            with self.subTest(mode=mode), transaction.atomic():
                instance = self.instance()
                detail.return_value = self.detail(instance)
                self.assertEqual(self.mutate(mode).status_code, 200)
                self.assertEqual(
                    detail.call_args.args[1], "Bearer cleanup-service-token"
                )
                if mode == "remove":
                    instance.refresh_from_db()
                    self.assertEqual(instance.status, "FAILED")
                transaction.set_rollback(True)

    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_only_explicit_null_target_allows_failed_instance(self, detail):
        for target in (str(uuid.uuid4()), "", False, 0, []):
            for mode in ("delete", "remove"):
                with self.subTest(target=target, mode=mode), transaction.atomic():
                    instance = self.instance()
                    detail.return_value = self.detail(
                        instance, runtime_target_id=target
                    )
                    self.assert_blocked(
                        self.mutate(mode), "ACTIVE_INSTANCE_EXISTS", instance
                    )
                    transaction.set_rollback(True)

    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_missing_or_mismatched_scope_is_not_cleanup_evidence(self, detail):
        instance = self.instance()
        valid = self.detail(instance)
        invalid = [None, [], "not an object"]
        for key in (
            "instance_id",
            "team_id",
            "challenge_id",
            "status",
            "runtime_target_id",
        ):
            invalid.append({k: v for k, v in valid.items() if k != key})
        for key in ("instance_id", "team_id", "challenge_id", "user_id"):
            invalid.append({**valid, key: str(uuid.uuid4())})
        invalid.extend(
            {**valid, "status": status}
            for status in ("RUNNING", "CLEANUP_PENDING", "CLEANED")
        )
        for body in invalid:
            for mode in ("delete", "remove"):
                with self.subTest(body=body, mode=mode):
                    detail.return_value = body
                    self.assert_blocked(
                        self.mutate(mode), "INSTANCE_CLEANUP_UNVERIFIED", instance
                    )

    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_release_id_must_match_when_recorded(self, detail):
        release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=1,
            registry_revision=1,
            isolation_profile="WEB",
            cpu_millicores=100,
            memory_mib=128,
            ephemeral_storage_mib=128,
        )
        instance = self.instance(release=release)
        for value in (None, str(uuid.uuid4())):
            detail.return_value = self.detail(instance, release_id=value)
            for mode in ("delete", "remove"):
                self.assert_blocked(
                    self.mutate(mode), "INSTANCE_CLEANUP_UNVERIFIED", instance
                )

    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_optional_user_id_may_be_omitted(self, detail):
        instance = self.instance()
        detail.return_value = {
            key: value
            for key, value in self.detail(instance).items()
            if key != "user_id"
        }
        self.assertEqual(self.mutate("remove").status_code, 200)

    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_scheduler_errors_do_not_allow_deletion(self, detail):
        instance = self.instance()
        for status in (401, 403, 404, 503):
            for mode in ("delete", "remove"):
                with self.subTest(status=status, mode=mode):
                    detail.side_effect = SchedulerError(
                        "PRIVATE_REMOTE_ERROR", "private-response", status
                    )
                    response = self.mutate(mode)
                    self.assert_blocked(
                        response, "INSTANCE_CLEANUP_UNVERIFIED", instance
                    )
                    self.assertNotIn("private-response", str(response.data))

    @override_settings(SCHEDULER_API_TOKEN="")
    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_missing_service_auth_does_not_use_admin_token(self, detail):
        instance = self.instance()
        self.assert_blocked(
            self.mutate("delete"), "INSTANCE_CLEANUP_UNVERIFIED", instance
        )
        detail.assert_not_called()

    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_pending_write_without_instance_blocks_mutation(self, detail):
        for status in ("STARTED", "RECOVERY_PENDING", "REVIEW_REQUIRED"):
            for action in ("CREATE", "RESET"):
                for mode in ("delete", "remove"):
                    with self.subTest(
                        status=status, action=action, mode=mode
                    ), transaction.atomic():
                        row = self.operation(status)
                        InstanceWriteOperation.objects.filter(pk=row.pk).update(
                            action=action
                        )
                        self.assert_blocked(
                            self.mutate(mode), "INSTANCE_REVIEW_REQUIRED"
                        )
                        self.assertTrue(
                            InstanceWriteOperation.objects.filter(pk=row.pk).exists()
                        )
                        transaction.set_rollback(True)
        detail.assert_not_called()

    def test_finished_write_is_preserved_but_does_not_block(self):
        for status in ("COMPLETED", "REJECTED"):
            for mode in ("delete", "remove"):
                with self.subTest(status=status, mode=mode), transaction.atomic():
                    row = self.operation(status)
                    self.assertEqual(self.mutate(mode).status_code, 200)
                    self.assertTrue(
                        InstanceWriteOperation.objects.filter(pk=row.pk).exists()
                    )
                    transaction.set_rollback(True)

    def test_unrelated_pending_write_does_not_block(self):
        self.operation("STARTED", user_id=self.elsewhere.pk, team_id=self.other.pk)
        self.assertEqual(self.mutate("delete").status_code, 200)

    def test_removing_member_does_not_block_on_another_member_write(self):
        self.operation("STARTED", user_id=self.leader.pk)
        self.assertEqual(self.mutate("remove").status_code, 200)

    def test_team_snapshot_of_moved_user_still_blocks_deletion(self):
        self.operation("REVIEW_REQUIRED", user_id=self.elsewhere.pk)
        self.assert_blocked(self.mutate("delete"), "INSTANCE_REVIEW_REQUIRED")

    def test_member_historical_records_are_not_cascaded_without_cleanup(self):
        instance = self.instance("STOPPED")
        Instance.objects.filter(pk=instance.pk).update(team=self.other)
        self.assert_blocked(self.mutate("delete"), "ACTIVE_INSTANCE_EXISTS", instance)

    def test_member_historical_pending_write_blocks_deletion(self):
        self.operation("REVIEW_REQUIRED", team_id=self.other.pk)
        self.assert_blocked(self.mutate("delete"), "INSTANCE_REVIEW_REQUIRED")

    @patch("apps.adminpanel.views.call_scheduler_detail")
    def test_all_instances_must_pass_before_any_mutation(self, detail):
        first = self.instance()
        second = self.instance()
        detail.side_effect = lambda row, auth: self.detail(
            row, runtime_target_id=None if row.pk == first.pk else str(uuid.uuid4())
        )
        self.assert_blocked(self.mutate("delete"), "ACTIVE_INSTANCE_EXISTS", second)
        self.assertEqual(
            Instance.objects.filter(pk__in=[first.pk, second.pk]).count(), 2
        )

    def test_new_owner_before_team_lock_restarts_with_owner_lock(self):
        User.objects.filter(pk=self.elsewhere.pk).update(team=self.team)
        owner_ids = views._team_instance_owner_ids
        calls = 0

        def stale_first_snapshot(team_id):
            nonlocal calls
            calls += 1
            result = owner_ids(team_id)
            if calls == 1:
                result.remove(self.elsewhere.pk)
            return result

        self.instance("RUNNING")
        with patch(
            "apps.adminpanel.views._team_instance_owner_ids",
            side_effect=stale_first_snapshot,
        ):
            self.assert_blocked(self.mutate("delete"), "ACTIVE_INSTANCE_EXISTS")
        self.assertEqual(calls, 4)
        self.assertTrue(InstanceLock.objects.filter(user=self.elsewhere).exists())


@override_settings(
    CACHES=fixtures.LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="cleanup-service-token",
)
class TeamCleanupRaceTests(Fixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=1,
            registry_revision=1,
            isolation_profile="WEB",
            cpu_millicores=100,
            memory_mib=128,
            ephemeral_storage_mib=128,
        )
        ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge, current_release=release
        )
        cell = Cell.objects.create(
            cell_index=1,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="cleanup QA",
        )
        TeamChallengeAccess.objects.create(
            team=self.team, challenge=self.challenge, source_cell=cell
        )
        self.release = release
        InstanceLock.objects.create(user=self.member)

    def client_request(self, mode, *, source=None):
        close_old_connections()
        try:
            client = APIClient()
            user_id = self.member.pk if mode == "write" else self.admin.pk
            client.force_authenticate(User.objects.get(pk=user_id))
            if mode == "write":
                path = (
                    f"/api/v1/instances/{source.pk}/reset"
                    if source
                    else "/api/v1/instances"
                )
                body = {} if source else {"challenge_id": str(self.challenge.pk)}
                return client.post(path, body, format="json")
            if mode == "delete":
                return client.delete(
                    self.url(), {"reason": "cleanup QA"}, format="json"
                )
            return client.patch(
                self.url(),
                {"reason": "cleanup QA", "remove_user_ids": [str(self.member.pk)]},
                format="json",
            )
        finally:
            connections.close_all()

    def assert_write_race(self, mode, *, reset=False, fail_save=False):
        entered, proceed, mutating, team_locked = Event(), Event(), Event(), Event()
        source = self.instance("RUNNING", release=self.release) if reset else None
        remote_id = uuid.uuid4()
        get_team = views._get_team_for_update

        def remote(*args, **kwargs):
            entered.set()
            if not proceed.wait(10):
                raise AssertionError("remote write was not released")
            return {"instance_id": str(remote_id), "status": "REQUESTED"}

        def team_lock(team_id):
            team_locked.set()
            return get_team(team_id)

        def mutate():
            mutating.set()
            return self.client_request(mode)

        save = views.create_instance_from_scheduler
        with patch(
            f"apps.instances.views.call_scheduler_{'reset' if reset else 'create'}",
            side_effect=remote,
        ) as call, patch(
            "apps.instances.views.create_instance_from_scheduler",
            side_effect=(
                IntegrityError("injected result save failure") if fail_save else save
            ),
        ), patch(
            "apps.adminpanel.views._get_team_for_update", side_effect=team_lock
        ), ThreadPoolExecutor(
            max_workers=2
        ) as pool:
            write = pool.submit(self.client_request, "write", source=source)
            try:
                self.assertTrue(entered.wait(10))
                mutation = pool.submit(mutate)
                self.assertTrue(mutating.wait(10))
                self.assertFalse(team_locked.wait(0.2))
            finally:
                proceed.set()
            self.assertEqual(write.result(15).status_code, 500 if fail_save else 202)
            response = mutation.result(15)
            self.assertEqual(response.status_code, 409, response.data)
        call.assert_called_once()
        self.member.refresh_from_db()
        self.assertEqual(self.member.team_id, self.team.pk)
        self.assertTrue(
            InstanceWriteOperation.objects.filter(remote_instance_id=remote_id).exists()
        )
        if not fail_save:
            self.assertTrue(Instance.objects.filter(pk=remote_id).exists())

    def test_create_and_team_delete_do_not_lose_remote_record(self):
        self.assert_write_race("delete")

    def test_reset_and_team_delete_do_not_lose_remote_record(self):
        self.assert_write_race("delete", reset=True)

    def test_create_and_member_removal_do_not_lose_remote_record(self):
        self.assert_write_race("remove")

    def test_reset_and_member_removal_do_not_lose_remote_record(self):
        self.assert_write_race("remove", reset=True)

    def test_failed_result_save_still_blocks_team_delete(self):
        self.assert_write_race("delete", fail_save=True)
