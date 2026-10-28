import json
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test import SimpleTestCase, TestCase, override_settings

from apps.adminpanel import tests as admin_existing
from apps.adminpanel.models import AdminEvent
from apps.instances import tests as existing
from apps.instances.models import Instance, InstanceStatus
from apps.instances.services import scheduler_auth_header, scheduler_request


@contextmanager
def scheduler_response(body, status=202, active=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, response_body, response_status):
            self.send_response(response_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.command, self.headers.get("Authorization")))
            self.respond(body, status)

        def do_GET(self):
            requests.append((self.command, self.headers.get("Authorization")))
            response_body = json.dumps({"data": active}).encode() if active else body
            self.respond(response_body, 200 if active else status)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with override_settings(SCHEDULER_BASE_URL=f"http://127.0.0.1:{server.server_port}"):
            yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@override_settings(CACHES=existing.LOCMEM, SCHEDULER_API_TOKEN="test-scheduler-token")
class SchedulerMalformedResponseTests(TestCase):
    setUp = existing.InstanceLockTests.setUp
    auth = existing.InstanceLockTests.auth

    def test_invalid_success_response_returns_503_without_creating_instance(self):
        bodies = (
            b'{"data":',
            b'<html>upstream unavailable</html>',
            b'{"data":"\xff"}',
            b'[]',
            b'null',
            b'false',
            b'42',
            b'"upstream"',
            b'{}',
        )
        for body in bodies:
            with self.subTest(body=body), scheduler_response(body) as requests:
                response = self.client.post(
                    "/api/v1/instances", {"challenge_id": str(self.challenge.pk)}, format="json",
                )
                self.assertEqual((response.status_code, response.data["code"]), (503, "SCHEDULER_UNAVAILABLE"))
                self.assertEqual(Instance.objects.count(), 0)
                self.assertEqual(requests, [("POST", "Bearer test-scheduler-token")])

    def test_malformed_response_after_acceptance_can_recover_the_same_instance(self):
        instance_id = str(uuid.uuid4())
        active = {
            "instance_id": instance_id, "user_id": str(self.user.pk),
            "team_id": str(self.team.pk), "challenge_id": str(self.challenge.pk),
            "release_id": str(self.release.pk), "registry_revision": 1,
            "status": "REQUESTED", "endpoints": [],
        }
        with scheduler_response(b'{"data":', active=active) as requests:
            failed = self.client.post(
                "/api/v1/instances", {"challenge_id": str(self.challenge.pk)}, format="json",
            )
            self.assertEqual(failed.status_code, 503)
            self.assertFalse(Instance.objects.exists())
            for _ in range(2):
                recovered = self.client.get("/api/v1/teams/me/instances")
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(recovered.data["data"]["instance_id"], instance_id)
            self.assertEqual(Instance.objects.count(), 1)
            self.assertEqual([method for method, _ in requests], ["POST", "GET", "GET"])

    def test_malformed_error_response_never_creates_an_instance(self):
        for status in (400, 401, 404, 409, 503):
            for body in (b'[]', b'null', b'{"message":"\xff"}', b'{"code":', b'{"code":[],"message":{}}'):
                with self.subTest(status=status, body=body), scheduler_response(body, status):
                    response = self.client.post(
                        "/api/v1/instances", {"challenge_id": str(self.challenge.pk)}, format="json",
                    )
                    self.assertEqual(response.status_code, 503 if status >= 500 else status)
                    self.assertIsInstance(response.data["code"], str)
                    self.assertIsInstance(response.data["message"], str)
                    self.assertFalse(Instance.objects.exists())

    def test_valid_scheduler_error_fields_keep_their_status(self):
        body = json.dumps({"code": "USER_INSTANCE_LIMIT_EXCEEDED", "message": "인스턴스 한도 초과"}).encode()
        with scheduler_response(body, 409):
            response = self.client.post(
                "/api/v1/instances", {"challenge_id": str(self.challenge.pk)}, format="json",
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "USER_INSTANCE_LIMIT_EXCEEDED")
        self.assertEqual(response.data["message"], "인스턴스 한도 초과")


@override_settings(SCHEDULER_API_TOKEN="test-scheduler-token")
class SchedulerSuccessEnvelopeTests(SimpleTestCase):
    def test_object_list_and_null_data_are_preserved(self):
        for data in ({"instance_id": str(uuid.uuid4())}, [{"event_type": "STATE_CHANGED"}], None):
            with self.subTest(data=data), scheduler_response(json.dumps({"data": data}).encode(), 200):
                self.assertEqual(scheduler_request("GET", "/api/instances", auth_header=scheduler_auth_header()), data)

    def test_empty_acknowledgement_is_preserved(self):
        with scheduler_response(b"", 204):
            self.assertIsNone(scheduler_request("POST", "/api/instances"))


@override_settings(CACHES=existing.LOCMEM, SCHEDULER_API_TOKEN="test-scheduler-token")
class SchedulerMalformedCleanupTests(TestCase):
    setUp = admin_existing.AdminInstanceTests.setUp
    auth = admin_existing.AdminInstanceTests.auth
    _instance = admin_existing.AdminInstanceTests._instance

    def test_malformed_retry_response_keeps_failed_state_and_records_failure(self):
        instance = self._instance(status=InstanceStatus.FAILED)
        with scheduler_response(b'{"data":') as requests:
            response = self.client.post(
                f"/api/v1/admin/instances/{instance.pk}/retry-cleanup", {}, format="json",
            )
        self.assertEqual((response.status_code, response.data["code"]), (503, "SCHEDULER_UNAVAILABLE"))
        instance.refresh_from_db()
        self.assertEqual(instance.status, InstanceStatus.FAILED)
        self.assertEqual(AdminEvent.objects.filter(instance_id=instance.pk).count(), 1)
        self.assertEqual(requests, [("POST", "Bearer test-scheduler-token")])
