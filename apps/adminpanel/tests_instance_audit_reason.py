import uuid
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, override_settings

from apps.adminpanel import tests as fixtures, views
from apps.adminpanel.models import AdminEvent
from apps.instances.models import Instance, InstanceStatus
from apps.instances.services import SchedulerError


@override_settings(
    CACHES=fixtures.LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="test-scheduler-token",
)
class InstanceAuditReasonTests(TestCase):
    setUp = fixtures.AdminInstanceTests.setUp
    auth = fixtures.AdminInstanceTests.auth
    _instance = fixtures.AdminInstanceTests._instance

    def submit(self, action, instance, body):
        path = f"/api/v1/admin/instances/{instance.pk}"
        if action == "reset":
            return self.client.post(path + "/reset", body, format="json")
        return self.client.delete(path, body, format="json")

    def snapshot(self, instance, status):
        return {
            "instance_id": str(instance.pk),
            "team_id": str(instance.team_id),
            "challenge_id": str(instance.challenge_id),
            "release_id": str(instance.release_id),
            "status": status,
        }

    def event(self, instance, reason, previous):
        event = AdminEvent.objects.get(type=AdminEvent.EventType.INSTANCE_FORCED)
        self.assertEqual(event.actor, self.admin.login_id)
        self.assertEqual(event.team_id, instance.team_id)
        self.assertEqual(event.challenge_id, instance.challenge_id)
        self.assertIn(reason, event.message)
        self.assertIn(previous, event.message)
        return event

    @patch("apps.adminpanel.views.call_scheduler_delete")
    @patch("apps.adminpanel.views.call_scheduler_reset")
    @patch("apps.adminpanel.views.call_scheduler_force_cleanup")
    def test_invalid_reason_does_not_call_scheduler_or_write_event(
        self, cleanup, reset, delete
    ):
        invalid = [
            None,
            [],
            "not an object",
            {},
            {"reason": None},
            {"reason": True},
            {"reason": 123},
            {"reason": []},
            {"reason": ""},
            {"reason": " \n\t "},
            {"reason": "x" * 501},
        ]
        for action in ("delete", "reset", "cleanup"):
            status = "CLEANUP_PENDING" if action == "cleanup" else "RUNNING"
            instance = self._instance(status=status)
            for body in invalid:
                with self.subTest(action=action, body=body):
                    response = self.submit(action, instance, body)
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertEqual(response.data["code"], "INVALID_REQUEST")
                    instance.refresh_from_db()
                    self.assertEqual(instance.status, status)
        for call in (cleanup, reset, delete):
            call.assert_not_called()
        self.assertFalse(AdminEvent.objects.exists())

    @patch("apps.adminpanel.views.call_scheduler_delete")
    @patch("apps.adminpanel.views.call_scheduler_reset")
    def test_missing_body_is_rejected(self, reset, delete):
        instance = self._instance()
        responses = [
            self.client.delete(f"/api/v1/admin/instances/{instance.pk}"),
            self.client.post(f"/api/v1/admin/instances/{instance.pk}/reset"),
        ]
        self.assertTrue(all(response.status_code == 400 for response in responses))
        reset.assert_not_called()
        delete.assert_not_called()
        self.assertFalse(AdminEvent.objects.exists())

    @patch("apps.adminpanel.views.call_scheduler_delete")
    def test_delete_reason_is_trimmed_and_preserved_in_event_api(self, delete):
        instance = self._instance()
        reason = "사유" + "x" * 498
        response = self.submit("delete", instance, {"reason": f" \t{reason}\n "})
        self.assertEqual(response.status_code, 202)
        event = self.event(instance, reason, "이전 상태: RUNNING")
        self.assertEqual(event.instance_id, instance.pk)
        self.assertEqual(event.severity, AdminEvent.Severity.WARNING)
        self.assertTrue(event.message.endswith(reason))
        rows = self.client.get("/api/v1/admin/events").data["data"]["events"]
        self.assertEqual(rows[0]["message"], event.message)
        delete.assert_called_once()

    @patch("apps.adminpanel.views.call_scheduler_delete")
    def test_one_character_and_unicode_length_boundary(self, delete):
        for reason in ("x", "\U0001f4dd" * 500):
            with self.subTest(length=len(reason)):
                instance = self._instance()
                response = self.submit("delete", instance, {"reason": reason})
                self.assertEqual(response.status_code, 202)
                self.assertTrue(
                    AdminEvent.objects.get(instance_id=instance.pk).message.endswith(
                        reason
                    )
                )

    @patch("apps.adminpanel.views.call_scheduler_reset")
    def test_reset_keeps_old_new_ids_state_and_reason_but_not_remote_secrets(
        self, reset
    ):
        old = self._instance()
        new_id = uuid.uuid4()
        reset.return_value = {
            **self.snapshot(old, "RESETTING"),
            "instance_id": str(new_id),
            "flag": "private-remote-flag",
            "token": "private-remote-token",
            "env": {"PASSWORD": "private-remote-password"},
        }
        response = self.submit("reset", old, {"reason": "  운영 재시작  "})
        self.assertEqual(response.status_code, 202, response.data)
        event = self.event(old, "운영 재시작", "(RUNNING)")
        self.assertEqual(event.instance_id, new_id)
        self.assertIn(str(old.pk), event.message)
        self.assertIn(str(new_id), event.message)
        self.assertNotIn("private-remote", event.message)
        self.assertTrue(event.message.endswith("운영 재시작"))

    @patch("apps.adminpanel.views.call_scheduler_force_cleanup")
    def test_cleanup_records_state_before_scheduler_update(self, cleanup):
        for remote_status in ("CLEANED", "FAILED"):
            with self.subTest(remote_status=remote_status):
                instance = self._instance(status="CLEANUP_PENDING")
                cleanup.return_value = self.snapshot(instance, remote_status)
                response = self.submit("cleanup", instance, {"reason": "정리 확인"})
                self.assertEqual(response.status_code, 200, response.data)
                event = AdminEvent.objects.get(instance_id=instance.pk)
                self.assertIn("이전 상태: CLEANUP_PENDING", event.message)
                self.assertIn("정리 확인", event.message)
                self.assertIn("인스턴스 강제 정리", event.message)
                instance.refresh_from_db()
                self.assertEqual(instance.status, remote_status)

    def test_scheduler_failures_keep_reason_and_previous_state(self):
        for action, remote in (
            ("delete", "delete"),
            ("reset", "reset"),
            ("cleanup", "force_cleanup"),
        ):
            with self.subTest(action=action):
                status = "CLEANUP_PENDING" if action == "cleanup" else "RUNNING"
                instance = self._instance(status=status)
                error = SchedulerError(
                    "SCHEDULER_UNAVAILABLE", "scheduler unavailable", 503
                )
                with patch(
                    f"apps.adminpanel.views.call_scheduler_{remote}", side_effect=error
                ):
                    response = self.submit(
                        action, instance, {"reason": "연결 실패 확인"}
                    )
                self.assertEqual(response.status_code, 503)
                event = self.event(instance, "연결 실패 확인", f"이전 상태: {status}")
                self.assertEqual(event.severity, AdminEvent.Severity.CRITICAL)
                instance.refresh_from_db()
                self.assertEqual(instance.status, status)
                AdminEvent.objects.all().delete()

    def test_event_write_failure_rolls_back_but_preserves_remote_result_and_reason(
        self,
    ):
        record_event = views._record_event
        for action, remote in (
            ("delete", "delete"),
            ("reset", "reset"),
            ("cleanup", "force_cleanup"),
        ):
            with self.subTest(action=action):
                status = "CLEANUP_PENDING" if action == "cleanup" else "RUNNING"
                instance = self._instance(status=status)
                new_id = uuid.uuid4()
                data = self.snapshot(
                    instance, "CLEANED" if action == "cleanup" else "RESETTING"
                )
                if action == "reset":
                    data["instance_id"] = str(new_id)
                calls = 0

                def fail_once(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise DatabaseError("injected audit failure")
                    return record_event(*args, **kwargs)

                with patch(
                    f"apps.adminpanel.views.call_scheduler_{remote}", return_value=data
                ) as call, patch(
                    "apps.adminpanel.views._record_event",
                    side_effect=fail_once,
                ):
                    response = self.submit(
                        action, instance, {"reason": "감사 실패 복구"}
                    )
                self.assertEqual(response.status_code, 500)
                call.assert_called_once()
                instance.refresh_from_db()
                self.assertEqual(instance.status, status)
                event = self.event(instance, "감사 실패 복구", f"이전 상태: {status}")
                self.assertEqual(event.instance_id, instance.pk)
                self.assertEqual(event.severity, AdminEvent.Severity.CRITICAL)
                self.assertIn("DB 반영 실패", event.message)
                if action == "reset":
                    self.assertIn(str(new_id), event.message)
                    self.assertFalse(Instance.objects.filter(pk=new_id).exists())
                AdminEvent.objects.all().delete()

    @patch("apps.adminpanel.views.call_scheduler_reset")
    def test_rejected_local_reset_still_keeps_remote_id_reason_and_state(self, reset):
        instance = self._instance()
        new_id = uuid.uuid4()
        reset.return_value = {"instance_id": str(new_id), "status": "RESETTING"}
        with patch(
            "apps.adminpanel.views.create_instance_from_scheduler",
            side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "scope rejected", 503),
        ):
            response = self.submit("reset", instance, {"reason": "원격 결과 확인"})
        self.assertEqual(response.status_code, 503)
        event = self.event(instance, "원격 결과 확인", "이전 상태: RUNNING")
        self.assertEqual(event.severity, AdminEvent.Severity.CRITICAL)
        self.assertIn(str(new_id), event.message)

    @patch("apps.adminpanel.views.call_scheduler_force_cleanup")
    def test_previous_state_is_read_after_owner_lock(self, cleanup):
        instance = self._instance()
        lock_owner = views._lock_instance_owner

        def update_before_locked_read(user):
            lock_owner(user)
            Instance.objects.filter(pk=instance.pk).update(status="CLEANUP_PENDING")

        cleanup.return_value = self.snapshot(instance, "CLEANED")
        with patch(
            "apps.adminpanel.views._lock_instance_owner",
            side_effect=update_before_locked_read,
        ):
            response = self.submit("delete", instance, {"reason": "잠금 뒤 확인"})
        self.assertEqual(response.status_code, 200)
        event = self.event(instance, "잠금 뒤 확인", "이전 상태: CLEANUP_PENDING")
        self.assertNotIn("RUNNING", event.message)

    @patch("apps.adminpanel.views.call_scheduler_retry_cleanup")
    def test_retry_cleanup_still_accepts_empty_object(self, retry):
        instance = self._instance(status=InstanceStatus.FAILED)
        retry.return_value = self.snapshot(instance, "CLEANUP_PENDING")
        response = self.client.post(
            f"/api/v1/admin/instances/{instance.pk}/retry-cleanup",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        retry.assert_called_once()
