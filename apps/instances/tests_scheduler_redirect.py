import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test import SimpleTestCase, override_settings

from apps.instances.services import SchedulerError, scheduler_request


class SchedulerRedirectTests(SimpleTestCase):
    """스케줄러 호출이 리다이렉트를 따라갈 때 토큰이 다른 호스트로 넘어가지 않는다 (QA TC-607)."""

    def start(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_redirect_to_other_host_is_not_followed(self):
        received = []

        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        other_port = self.start(Other)

        class Scheduler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{other_port}{self.path}")
                self.end_headers()

            def log_message(self, *args):
                pass

        port = self.start(Scheduler)
        with override_settings(SCHEDULER_BASE_URL=f"http://127.0.0.1:{port}"):
            with self.assertRaises(SchedulerError) as raised:
                scheduler_request("GET", "/v1/instances/x", auth_header="Bearer secret-token")

        self.assertEqual(raised.exception.code, "SCHEDULER_UNAVAILABLE")
        self.assertEqual(received, [])

    def test_redirect_within_same_host_keeps_working(self):
        received = []

        class Scheduler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/old"):
                    self.send_response(301)
                    self.send_header("Location", self.path[len("/old"):])
                    self.end_headers()
                    return
                received.append(self.headers.get("Authorization"))
                body = json.dumps({"code": "SUCCESS", "message": "ok", "data": {"ok": True}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        port = self.start(Scheduler)
        with override_settings(SCHEDULER_BASE_URL=f"http://127.0.0.1:{port}/old"):
            data = scheduler_request("GET", "/v1/instances/x", auth_header="Bearer secret-token")

        self.assertEqual(data, {"ok": True})
        self.assertEqual(received, ["Bearer secret-token"])
