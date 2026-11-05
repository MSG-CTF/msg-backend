from types import SimpleNamespace
from unittest.mock import patch

from django.db import DatabaseError, connection
from django.test import TestCase, TransactionTestCase, override_settings

from apps.adminpanel import tests_instance_cleanup as cleanup
from apps.adminpanel import views
from apps.adminpanel.models import AdminEvent
from apps.instances.models import Instance, InstanceStatus
from apps.instances.reconciliation import reconcile_instances_once
from apps.instances.services import SchedulerError


class CleanupRetryFixtures:
    setUp = cleanup.AdminCleanupRetryTests.setUp
    auth = cleanup.AdminCleanupRetryTests.auth
    _instance = cleanup.AdminCleanupRetryTests._instance
    retry = cleanup.AdminCleanupRetryTests.retry
    snapshot = cleanup.AdminCleanupRetryTests.snapshot

    def assert_recovery_event(self, instance):
        event = AdminEvent.objects.get(instance_id=instance.pk)
        self.assertEqual(event.severity, AdminEvent.Severity.CRITICAL)
        self.assertEqual(event.actor, "root")
        self.assertEqual(event.team_id, self.team.pk)
        self.assertEqual(event.challenge_id, self.challenge.pk)
        self.assertIn("정리 재시도", event.message)
        self.assertIn("DB 반영 실패", event.message)
        self.assertIn("CLEANUP_PENDING", event.message)
        self.assertIn("FAILED", event.message)
        self.assertNotIn("private-", event.message)
        return event


@override_settings(
    CACHES=cleanup.existing.LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="test-scheduler-token",
)
class CleanupRetryRecoveryTests(CleanupRetryFixtures, TestCase):
    def test_shared_recovery_helper_accepts_reason_and_previous_status_context(self):
        instance = self._instance(status=InstanceStatus.FAILED)
        views._record_remote_applied(
            SimpleNamespace(user=self.admin),
            instance,
            "정리 재시도",
            "CLEANUP_PENDING",
            reason="local-recovery-reason",
            previous_status="FAILED",
        )
        event = self.assert_recovery_event(instance)
        self.assertIn("local-recovery-reason", event.message)

    @patch("apps.instances.services.scheduler_request")
    def test_save_failure_records_acceptance_and_reconciliation_does_not_resubmit(
        self, remote
    ):
        instance = self._instance(status=InstanceStatus.FAILED)
        before = Instance.objects.values().get(pk=instance.pk)
        remote.return_value = self.snapshot(instance, "CLEANUP_PENDING")
        with patch.object(
            Instance, "save", side_effect=DatabaseError("private-save-error")
        ):
            response = self.retry(instance.pk)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-", response.content.decode())
        self.assertEqual(Instance.objects.values().get(pk=instance.pk), before)
        event = self.assert_recovery_event(instance)
        with patch(
            "apps.instances.reconciliation.call_scheduler_detail",
            return_value=self.snapshot(instance, "CLEANED"),
        ):
            result = reconcile_instances_once(instance_ids=[instance.pk])
        self.assertEqual(result["updated"], 1)
        instance.refresh_from_db()
        self.assertEqual(instance.status, InstanceStatus.CLEANED)
        self.assertEqual(instance.release_id, before["release_id"])
        self.assertTrue(AdminEvent.objects.filter(pk=event.pk).exists())
        remote.assert_called_once()

    @patch("apps.instances.services.scheduler_request")
    def test_audit_failure_rolls_back_success_and_records_separate_recovery_event(
        self, remote
    ):
        instance = self._instance(status=InstanceStatus.FAILED)
        before = Instance.objects.values().get(pk=instance.pk)
        remote.return_value = self.snapshot(instance, "CLEANUP_PENDING")
        record = views._record_event
        calls = []

        def fail_first(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise DatabaseError("private-audit-error")
            return record(*args, **kwargs)

        with patch("apps.adminpanel.views._record_event", side_effect=fail_first):
            response = self.retry(instance.pk)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(calls), 2)
        self.assertEqual(Instance.objects.values().get(pk=instance.pk), before)
        self.assert_recovery_event(instance)
        remote.assert_called_once()

    @patch("apps.instances.services.scheduler_request")
    def test_post_acceptance_validation_failure_is_not_reported_as_remote_rejection(
        self, remote
    ):
        instance = self._instance(status=InstanceStatus.FAILED)
        remote.return_value = self.snapshot(instance, "CLEANUP_PENDING")
        with patch(
            "apps.adminpanel.views.update_instance_from_scheduler",
            side_effect=SchedulerError(
                "SCHEDULER_UNAVAILABLE",
                "정리 결과를 확인할 수 없습니다",
                503,
            ),
        ):
            response = self.retry(instance.pk)
        self.assertEqual(
            (response.status_code, response.data["code"]),
            (503, "SCHEDULER_UNAVAILABLE"),
        )
        instance.refresh_from_db()
        self.assertEqual(instance.status, InstanceStatus.FAILED)
        self.assert_recovery_event(instance)
        remote.assert_called_once()

    @patch(
        "apps.adminpanel.views.call_scheduler_retry_cleanup",
        side_effect=RuntimeError("local-call-error"),
    )
    def test_exception_before_acceptance_does_not_fabricate_a_receipt(self, remote):
        instance = self._instance(status=InstanceStatus.FAILED)
        response = self.retry(instance.pk)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(AdminEvent.objects.filter(instance_id=instance.pk).exists())
        instance.refresh_from_db()
        self.assertEqual(instance.status, InstanceStatus.FAILED)
        remote.assert_called_once()

    @patch(
        "apps.instances.services.scheduler_request",
        side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "unavailable", 503),
    )
    def test_unconfirmed_remote_result_keeps_failure_audit_without_claiming_acceptance(
        self, remote
    ):
        instance = self._instance(status=InstanceStatus.FAILED)
        response = self.retry(instance.pk)
        self.assertEqual(response.status_code, 503)
        event = AdminEvent.objects.get(instance_id=instance.pk)
        self.assertIn("SCHEDULER_UNAVAILABLE", event.message)
        self.assertNotIn("DB 반영 실패", event.message)
        remote.assert_called_once()

    @patch("apps.instances.services.scheduler_request")
    def test_persistent_database_failure_keeps_log_evidence_without_resubmitting(
        self, remote
    ):
        instance = self._instance(status=InstanceStatus.FAILED)
        remote.return_value = self.snapshot(instance, "CLEANUP_PENDING")
        with patch.object(
            Instance, "save", side_effect=DatabaseError("private-save-error")
        ), patch(
            "apps.adminpanel.models.AdminEvent.objects.create",
            side_effect=DatabaseError("private-audit-error"),
        ), self.assertLogs(
            "apps.adminpanel.views", level="ERROR"
        ) as logged:
            response = self.retry(instance.pk)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-", response.content.decode())
        self.assertTrue(any(str(instance.pk) in message for message in logged.output))
        self.assertFalse(AdminEvent.objects.exists())
        remote.assert_called_once()


@override_settings(
    CACHES=cleanup.existing.LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="test-scheduler-token",
)
class CleanupRetryCommitTests(CleanupRetryFixtures, TransactionTestCase):
    @patch("apps.instances.services.scheduler_request")
    def test_result_commit_failure_records_receipt_after_rollback(self, remote):
        instance = self._instance(status=InstanceStatus.FAILED)
        before = Instance.objects.values().get(pk=instance.pk)
        remote.return_value = self.snapshot(instance, "CLEANUP_PENDING")
        commit = connection.commit
        calls = 0

        def fail_first_commit():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DatabaseError("private-commit-error")
            return commit()

        with patch.object(connection, "commit", side_effect=fail_first_commit):
            response = self.retry(instance.pk)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(calls, 2)
        self.assertEqual(Instance.objects.values().get(pk=instance.pk), before)
        self.assert_recovery_event(instance)
        remote.assert_called_once()
