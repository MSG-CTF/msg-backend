"""Contract checks for the single approved Windows EXE trial helper."""

import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tools import windows_one_submission_smoke as smoke


class OneSubmissionSmokeTests(unittest.TestCase):
    def test_posts_once_then_reads_result_and_cleanup(self):
        challenge_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
        submission_id = uuid.UUID("00000000-0000-4000-8000-000000000002")
        idempotency_key = uuid.UUID("00000000-0000-4000-8000-000000000003")
        requests = []

        def answer(request, timeout):
            self.assertEqual(timeout, 20)
            requests.append(request)
            if request.get_method() == "POST":
                self.assertEqual(request.get_header("Authorization"), "Bearer participant-token")
                self.assertEqual(request.get_header("Idempotency-key"), str(idempotency_key))
                self.assertIn(b'name="file"', request.data)
                self.assertIn(b"MZharmless", request.data)
                body = {"data": {"submission_id": str(submission_id)}}
            else:
                self.assertEqual(request.get_header("Authorization"), "Bearer participant-token")
                body = {"data": {"status": "CLEANUP_PENDING", "execution_status": "COMPLETED",
                                 "exit_code": 0, "output": {"stdout": "ok", "stderr": ""},
                                 "cleanup_complete": len(requests) == 3}}
            return io.BytesIO(json.dumps(body).encode())

        with tempfile.TemporaryDirectory() as temporary:
            exe = Path(temporary) / "canary.exe"
            token_file = Path(temporary) / "token.txt"
            exe.write_bytes(b"MZharmless")
            token_file.write_text("participant-token\n", encoding="utf-8")
            with patch.object(smoke.OPENER, "open", side_effect=answer), patch.object(smoke.time, "sleep"):
                result = smoke.main([
                    "--backend-origin", "https://msgctf.kr",
                    "--challenge-id", str(challenge_id),
                    "--exe-path", str(exe),
                    "--access-token-file", str(token_file),
                    "--idempotency-key", str(idempotency_key),
                ])

        self.assertEqual(result, 0)
        self.assertEqual([request.get_method() for request in requests], ["POST", "GET", "GET"])
        self.assertEqual(requests[0].full_url,
                         f"https://msgctf.kr/api/v1/challenges/{challenge_id}/exe-submissions")
        self.assertEqual(requests[1].full_url, requests[0].full_url + f"/{submission_id}")

    def test_redirect_is_not_followed_with_participant_token(self):
        self.assertIsNone(smoke.NoRedirect().redirect_request(None, None, 307, "redirect", {}, "https://other.example"))


if __name__ == "__main__":
    unittest.main()
