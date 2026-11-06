import json
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from apps.instances.models import Instance, InstanceWriteOperation
from apps.instances.services import SchedulerError, scheduler_request
from apps.instances.tests_write_recovery import Fixtures
from apps.instances.write_recovery import (
    record_write_failure, recover_pending_writes_once, recover_write,
)
from apps.accounts.models import User, Role
from apps.adminpanel.models import AdminEvent


@override_settings(SECURE_SSL_REDIRECT=False, SCHEDULER_API_TOKEN="local-request-recovery",
                   SCHEDULER_REQUEST_RECOVERY_ENABLED=True)
class InstanceRequestRecoveryTests(Fixtures, TestCase):
    def lost_write(self, source=None):
        name = "call_scheduler_reset" if source else "call_scheduler_create"
        with patch("apps.instances.views." + name, side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "response lost", 503)) as remote:
            response = self.post(source)
        self.assertEqual(response.status_code, 503)
        row = InstanceWriteOperation.objects.latest("created_at")
        self.assertTrue(row.request_id_used)
        self.assertEqual(row.status, "RECOVERY_PENDING")
        self.assertEqual(remote.call_args.kwargs["request_id"], row.pk)
        self.assertIsNone(row.remote_instance_id)
        return row

    def result(self, row, **changes):
        return {
            "request_id": str(row.pk), "user_id": str(self.user.pk), "action": row.action,
            "source_instance_id": str(row.source_instance_id) if row.source_instance_id else None,
            "instance": self.detail(row, instance_id=str(uuid.uuid4())), **changes,
        }

    def test_lost_create_response_retries_original_result_without_another_post(self):
        row = self.lost_write()
        result = self.result(row)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", return_value=result) as lookup, patch("apps.instances.views.call_scheduler_create") as create:
            for _ in range(3):
                response = self.post()
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.data["data"]["instance_id"], result["instance"]["instance_id"])
        create.assert_not_called()
        lookup.assert_called_once()
        self.assertEqual(InstanceWriteOperation.objects.count(), 1)
        self.assertEqual(Instance.objects.count(), 1)

    def test_lost_reset_response_and_late_retry_do_not_reset_the_replacement(self):
        source = self.old_instance()
        row = self.lost_write(source)
        result = self.result(row)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", return_value=result), patch("apps.instances.views.call_scheduler_reset") as reset:
            response = self.post(source)
            self.assertEqual(response.status_code, 202)
            Instance.objects.filter(pk=result["instance"]["instance_id"]).update(status="CLEANED")
            late = self.post(source)
            self.assertEqual(late.status_code, 202)
            self.assertEqual(late.data["data"]["instance_id"], result["instance"]["instance_id"])
        reset.assert_not_called()
        self.assertEqual(Instance.objects.count(), 2)
        source.refresh_from_db()
        self.assertEqual(source.status, "STOPPING")

    def test_background_task_recovers_without_a_screen_query(self):
        row = self.lost_write()
        result = self.result(row)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", return_value=result):
            summary = recover_pending_writes_once()
        self.assertEqual(summary, {"checked": 1, "completed": 1, "pending": 0, "review": 0})
        row.refresh_from_db()
        self.assertEqual(str(row.remote_instance_id), result["instance"]["instance_id"])

    def test_recent_not_found_waits_without_reissuing_and_later_recovers(self):
        row = self.lost_write()
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=SchedulerError("INSTANCE_REQUEST_NOT_FOUND", "not visible", 404)), patch("apps.instances.views.call_scheduler_create") as create:
            response = self.post()
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["code"], "INSTANCE_OPERATION_PENDING")
        create.assert_not_called()
        with patch("apps.instances.write_recovery.call_scheduler_request_result", return_value=self.result(row)):
            self.assertEqual(recover_write(row.pk), "COMPLETED")

    def test_missing_result_after_deadline_requires_review(self):
        row = self.lost_write()
        InstanceWriteOperation.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(minutes=11))
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=SchedulerError("INSTANCE_REQUEST_NOT_FOUND", "not found", 404)):
            self.assertEqual(recover_write(row.pk), "REVIEW_REQUIRED")
        self.assertFalse(Instance.objects.exists())

    def test_wrong_request_owner_action_source_and_release_never_adopted(self):
        row = self.lost_write()
        valid = self.result(row)
        mutations = [
            {"request_id": str(uuid.uuid4())}, {"user_id": str(uuid.uuid4())},
            {"action": "RESET"}, {"source_instance_id": str(uuid.uuid4())},
        ] + [{"instance": {**valid["instance"], field: str(uuid.uuid4())}} for field in ("team_id", "challenge_id", "release_id", "user_id")]
        for change in mutations:
            with self.subTest(change=change):
                InstanceWriteOperation.objects.filter(pk=row.pk).update(status="RECOVERY_PENDING", remote_instance_id=None)
                with patch("apps.instances.write_recovery.call_scheduler_request_result", return_value={**valid, **change}):
                    self.assertEqual(recover_write(row.pk), "REVIEW_REQUIRED")
                self.assertFalse(Instance.objects.exists())

    def test_reset_result_cannot_point_to_another_source(self):
        row = self.lost_write(self.old_instance())
        with patch("apps.instances.write_recovery.call_scheduler_request_result", return_value=self.result(row, source_instance_id=str(uuid.uuid4()))):
            self.assertEqual(recover_write(row.pk), "REVIEW_REQUIRED")
        self.assertEqual(Instance.objects.count(), 1)

    def test_stale_started_request_recovers_after_process_interruption(self):
        row = self.lost_write()
        InstanceWriteOperation.objects.filter(pk=row.pk).update(status="STARTED", updated_at=timezone.now() - timedelta(seconds=20))
        with patch("apps.instances.write_recovery.call_scheduler_request_result", return_value=self.result(row)):
            self.assertEqual(recover_write(row.pk), "COMPLETED")

    def test_live_started_request_is_not_claimed_by_the_reconciler(self):
        row = self.lost_write()
        InstanceWriteOperation.objects.filter(pk=row.pk).update(status="STARTED")
        with patch("apps.instances.write_recovery.call_scheduler_request_result") as lookup:
            self.assertEqual(recover_write(row.pk), "STARTED")
        lookup.assert_not_called()

    def test_legacy_ambiguous_write_is_never_adopted_from_an_active_instance(self):
        row = self.lost_write()
        InstanceWriteOperation.objects.filter(pk=row.pk).update(request_id_used=False, status="REVIEW_REQUIRED")
        with patch("apps.instances.write_recovery.call_scheduler_request_result") as lookup:
            self.assertEqual(recover_write(row.pk, review=True), "REVIEW_REQUIRED")
        lookup.assert_not_called()

    def test_lookup_outage_preserves_pending_and_team_change_prevents_adoption(self):
        row = self.lost_write()
        with patch("apps.instances.write_recovery.call_scheduler_request_result", side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "unavailable", 503)):
            self.assertEqual(recover_write(row.pk), "RECOVERY_PENDING")
        self.user.team = None
        self.user.save(update_fields=["team"])
        with patch("apps.instances.write_recovery.call_scheduler_request_result") as lookup:
            self.assertEqual(recover_write(row.pk), "REVIEW_REQUIRED")
        lookup.assert_not_called()

    def test_local_database_failure_keeps_verified_remote_id_for_retry(self):
        row = self.lost_write()
        result = self.result(row)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", return_value=result), patch("apps.instances.services.Instance.objects.update_or_create", side_effect=IntegrityError("injected local failure")):
            self.assertEqual(recover_write(row.pk), "RECOVERY_PENDING")
        row.refresh_from_db()
        self.assertEqual(str(row.remote_instance_id), result["instance"]["instance_id"])
        with patch("apps.instances.write_recovery.call_scheduler_detail", return_value=result["instance"]), patch("apps.instances.write_recovery.call_scheduler_request_result") as lookup:
            self.assertEqual(recover_write(row.pk), "COMPLETED")
        lookup.assert_not_called()

    def test_late_failure_handler_cannot_overwrite_a_completed_operation(self):
        row = self.lost_write()
        with patch("apps.instances.write_recovery.call_scheduler_request_result", return_value=self.result(row)):
            self.assertEqual(recover_write(row.pk), "COMPLETED")
        record_write_failure(row, SchedulerError("SCHEDULER_UNAVAILABLE", "late error", 503), sent=True)
        row.refresh_from_db()
        self.assertEqual(row.status, "COMPLETED")

    def test_admin_reset_uses_the_same_durable_recovery_and_audits_the_request(self):
        source = self.old_instance()
        admin = User.objects.create_user(login_id="request-recovery-admin", role=Role.ADMIN)
        self.client.force_authenticate(admin)
        path = f"/api/v1/admin/instances/{source.pk}/reset"
        with patch("apps.adminpanel.views.call_scheduler_reset", side_effect=SchedulerError("SCHEDULER_UNAVAILABLE", "lost", 503)) as remote:
            self.assertEqual(self.client.post(path, {}, format="json").status_code, 503)
        row = InstanceWriteOperation.objects.get()
        self.assertEqual(row.user_id, self.user.pk)
        self.assertEqual(remote.call_args.kwargs["request_id"], row.pk)
        result = self.result(row)
        with patch("apps.instances.write_recovery.call_scheduler_request_result", return_value=result), patch("apps.adminpanel.views.call_scheduler_reset") as reset:
            for _ in range(2):
                response = self.client.post(path, {}, format="json")
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.data["data"]["instance_id"], result["instance"]["instance_id"])
        reset.assert_not_called()
        self.assertEqual(InstanceWriteOperation.objects.count(), 1)
        self.assertEqual(AdminEvent.objects.filter(message__contains=str(row.pk)).count(), 1)


@contextmanager
def delayed_scheduler(delay):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            time.sleep(delay)
            raw = json.dumps({"data": {"accepted": True}}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        do_GET = respond
        do_POST = respond
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with override_settings(SCHEDULER_BASE_URL=f"http://127.0.0.1:{server.server_port}"):
            yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@override_settings(SCHEDULER_TIMEOUT_SECONDS=0.15, SCHEDULER_WRITE_TIMEOUT_SECONDS=1)
class SchedulerWriteTimeoutTests(SimpleTestCase):
    def test_write_waits_longer_than_the_read_deadline(self):
        with delayed_scheduler(0.35):
            self.assertEqual(scheduler_request("POST", "/api/instances"), {"accepted": True})
            with self.assertRaises(SchedulerError):
                scheduler_request("GET", "/api/instances")

    def test_explicit_lookup_deadline_is_honored(self):
        with delayed_scheduler(0.35):
            self.assertEqual(scheduler_request("GET", "/api/instances/requests/test", timeout=1), {"accepted": True})
