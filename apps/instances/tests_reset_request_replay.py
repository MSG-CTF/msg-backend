import uuid
from datetime import timedelta
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.db import close_old_connections
from rest_framework.test import APIClient
from apps.accounts.models import User, Role
from django.utils import timezone

from apps.instances.models import Instance, InstanceWriteOperation
from apps.instances.services import SchedulerError
from apps.instances import tests_request_recovery as request_fixtures
from apps.instances.tests_write_recovery import Fixtures
from apps.instances.write_recovery import recover_write


@override_settings(SECURE_SSL_REDIRECT=False, SCHEDULER_API_TOKEN="reset-replay-test",
                   SCHEDULER_REQUEST_RECOVERY_ENABLED=True)
class ResetRequestReplayTests(Fixtures, TestCase):
    lost_write = request_fixtures.InstanceRequestRecoveryTests.lost_write
    result = request_fixtures.InstanceRequestRecoveryTests.result

    def missing(self):
        return SchedulerError("INSTANCE_REQUEST_NOT_FOUND", "not accepted", 404)

    def source_detail(self, row, status="RUNNING", **changes):
        return self.detail(row, instance_id=str(row.source_instance_id), status=status, **changes)

    def test_missing_reset_is_replayed_with_the_original_source_and_request_id(self):
        source = self.old_instance()
        row = self.lost_write(source)
        result = self.result(row)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=[self.missing(), result]) as lookup, \
             patch("apps.instances.write_recovery.call_scheduler_detail", return_value=self.source_detail(row)), \
             patch("apps.instances.write_recovery.call_scheduler_reset", return_value=result["instance"]) as reset:
            self.assertEqual(recover_write(row.pk), "COMPLETED")
            self.assertEqual(recover_write(row.pk), "COMPLETED")
        self.assertEqual(lookup.call_count, 2)
        reset.assert_called_once()
        self.assertEqual(reset.call_args.args[0].pk, source.pk)
        self.assertEqual(reset.call_args.kwargs["request_id"], row.pk)
        self.assertEqual(InstanceWriteOperation.objects.count(), 1)
        self.assertEqual(Instance.objects.count(), 2)

    def test_a_committed_started_journal_survives_a_process_exit_before_send(self):
        source = self.old_instance()
        row = self.lost_write(source)
        InstanceWriteOperation.objects.filter(pk=row.pk).update(status="STARTED", updated_at=timezone.now()-timedelta(seconds=30))
        result = self.result(row)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=[self.missing(), result]), \
             patch("apps.instances.write_recovery.call_scheduler_detail", return_value=self.source_detail(row)), \
             patch("apps.instances.write_recovery.call_scheduler_reset", return_value=result["instance"]) as reset:
            response = self.post(source)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["data"]["operation_id"], str(row.pk))
        self.assertEqual(response.data["data"]["instance_id"], result["instance"]["instance_id"])
        self.assertEqual(reset.call_args.kwargs["request_id"], row.pk)

    def test_delete_winning_before_acceptance_rejects_the_reset_and_unblocks_creation(self):
        source = self.old_instance()
        row = self.lost_write(source)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=self.missing()), \
             patch("apps.instances.write_recovery.call_scheduler_detail", return_value=self.source_detail(row, "CLEANED")), \
             patch("apps.instances.write_recovery.call_scheduler_active", side_effect=SchedulerError("INSTANCE_NOT_FOUND", "none active", 404)), \
             patch("apps.instances.write_recovery.call_scheduler_reset") as reset:
            self.assertEqual(recover_write(row.pk), "REJECTED")
        reset.assert_not_called()
        row.refresh_from_db();source.refresh_from_db()
        self.assertEqual(row.last_error_code, "RESET_SOURCE_CLEANED")
        self.assertEqual(source.status, "CLEANED")
        with patch("apps.instances.views.call_scheduler_create", return_value=self.detail(row, instance_id=str(uuid.uuid4()))):
            self.assertEqual(self.post().status_code, 202)

    def test_a_stopping_source_waits_without_redelivery_until_cleanup_is_proven(self):
        source = self.old_instance()
        row = self.lost_write(source)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=self.missing()), \
             patch("apps.instances.write_recovery.call_scheduler_detail", return_value=self.source_detail(row, "STOPPING")), \
             patch("apps.instances.write_recovery.call_scheduler_reset") as reset:
            self.assertEqual(recover_write(row.pk), "RECOVERY_PENDING")
        reset.assert_not_called()

    def test_cleanup_with_an_active_instance_does_not_guess_that_the_request_was_cancelled(self):
        source = self.old_instance()
        row = self.lost_write(source)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=self.missing()), \
             patch("apps.instances.write_recovery.call_scheduler_detail", return_value=self.source_detail(row, "CLEANED")), \
             patch("apps.instances.write_recovery.call_scheduler_active", return_value={"instance_id": str(uuid.uuid4())}), \
             patch("apps.instances.write_recovery.call_scheduler_reset") as reset:
            self.assertEqual(recover_write(row.pk), "REVIEW_REQUIRED")
        reset.assert_not_called()

    def test_another_teams_source_response_cannot_trigger_a_reset(self):
        source = self.old_instance()
        row = self.lost_write(source)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=self.missing()), \
             patch("apps.instances.write_recovery.call_scheduler_detail", return_value=self.source_detail(row, team_id=str(uuid.uuid4()))), \
             patch("apps.instances.write_recovery.call_scheduler_reset") as reset:
            self.assertEqual(recover_write(row.pk), "REVIEW_REQUIRED")
        reset.assert_not_called()

    def test_banning_a_team_before_recovery_does_not_redeliver_the_write(self):
        source = self.old_instance()
        row = self.lost_write(source)
        self.team.is_banned = True
        self.team.save(update_fields=["is_banned"])
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=self.missing()), \
             patch("apps.instances.write_recovery.call_scheduler_reset") as reset:
            self.assertEqual(recover_write(row.pk), "REJECTED")
        reset.assert_not_called()

    def test_a_transient_lookup_error_does_not_redeliver_a_possibly_accepted_write(self):
        source = self.old_instance()
        row = self.lost_write(source)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "timeout", 503)), \
             patch("apps.instances.write_recovery.call_scheduler_reset") as reset:
            self.assertEqual(recover_write(row.pk), "RECOVERY_PENDING")
        reset.assert_not_called()

    def test_create_is_never_reconstructed_from_mutable_current_configuration(self):
        row = self.lost_write()
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=self.missing()), \
             patch("apps.instances.write_recovery.call_scheduler_reset") as reset:
            self.assertEqual(recover_write(row.pk), "RECOVERY_PENDING")
        reset.assert_not_called()


@skipUnlessDBFeature("has_select_for_update_of")
@override_settings(SECURE_SSL_REDIRECT=False, SCHEDULER_API_TOKEN="reset-lock-test", SCHEDULER_REQUEST_RECOVERY_ENABLED=True)
class IndependentResetLockTests(Fixtures, TransactionTestCase):
    def test_same_challenge_users_can_both_reach_scheduler_before_either_reply(self):
        source = self.old_instance()
        other = User.objects.create_user(login_id="independent-reset", role=Role.PARTICIPANT, team=self.team)
        second = Instance.objects.create(user=other, team=self.team, challenge=self.challenge, release=self.release, status="RUNNING")
        entered = Barrier(2)

        def remote(instance, *args, **kwargs):
            entered.wait(timeout=4)
            return {"instance_id": str(uuid.uuid4()), "user_id": str(instance.user_id), "team_id": str(instance.team_id),
                    "challenge_id": str(instance.challenge_id), "release_id": str(instance.release_id),
                    "replaced_instance_id": str(instance.pk), "status": "CREATING"}

        def reset(user_id, instance_id):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=user_id))
                return client.post(f"/api/v1/instances/{instance_id}/reset", {}, format="json").status_code
            finally:
                close_old_connections()

        with patch("apps.instances.views.call_scheduler_reset", side_effect=remote), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(reset, self.user.pk, source.pk)
            next_result = pool.submit(reset, other.pk, second.pk)
            self.assertEqual([first.result(timeout=15), next_result.result(timeout=15)], [202, 202])
