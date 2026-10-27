"""Submit one approved Windows EXE and poll its RESULT and CLEANUP state.

The participant access token is read from a local file and never printed.
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


TERMINAL = {"COMPLETED", "CRASH", "TIMEOUT", "INFRA_ERROR", "START_UNKNOWN"}
MAX_EXE_BYTES = 10 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)


def api_json(request):
    try:
        with OPENER.open(request, timeout=20) as response:  # nosec B310: validated HTTPS origin
            return json.load(response)
    except HTTPError as error:
        try:
            detail = json.loads(error.read(4096)).get("code", "HTTP_ERROR")
        except (ValueError, UnicodeError):
            detail = "HTTP_ERROR"
        raise RuntimeError(f"Backend HTTP {error.code}: {detail}") from None
    except URLError as error:
        raise RuntimeError(f"Backend connection failed: {error.reason}") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-origin", required=True, help="HTTPS Backend origin, e.g. https://msgctf.kr")
    parser.add_argument("--challenge-id", required=True, help="Windows challenge UUID")
    parser.add_argument("--exe-path", required=True, type=Path, help="Approved harmless EXE")
    parser.add_argument("--access-token-file", required=True, type=Path, help="Local participant access token file")
    parser.add_argument("--idempotency-key", type=uuid.UUID, default=None, help="Reuse the same UUID after an uncertain POST")
    parser.add_argument("--max-wait-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=int, default=3)
    args = parser.parse_args(argv)

    origin = urlsplit(args.backend_origin)
    if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.path not in ("", "/") or origin.query or origin.fragment:
        parser.error("--backend-origin must be an HTTPS origin without credentials, path, or query")
    if args.max_wait_seconds < 1 or args.poll_seconds < 1:
        parser.error("wait and poll intervals must be positive")
    try:
        challenge_id = uuid.UUID(args.challenge_id)
    except ValueError:
        parser.error("--challenge-id must be a UUID")
    if args.exe_path.suffix.lower() != ".exe" or not args.exe_path.is_file() or args.exe_path.stat().st_size > MAX_EXE_BYTES:
        parser.error("--exe-path must be an existing .exe of at most 10 MiB")
    token = args.access_token_file.read_text(encoding="utf-8").strip()
    if not token or any(char.isspace() for char in token):
        parser.error("access token file must contain one token")

    key = args.idempotency_key or uuid.uuid4()
    endpoint = args.backend_origin.rstrip("/") + f"/api/v1/challenges/{challenge_id}/exe-submissions"
    boundary = "msgctf-" + uuid.uuid4().hex
    header = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="trial.exe"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii")
    body = header + args.exe_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("ascii")
    common_headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    print(f"idempotency_key={key}", flush=True)
    accepted = api_json(Request(endpoint, data=body, method="POST", headers={
        **common_headers,
        "Idempotency-Key": str(key),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }))
    submission_id = uuid.UUID(accepted["data"]["submission_id"])
    print(f"submission_id={submission_id}", flush=True)

    detail_url = endpoint + f"/{submission_id}"
    deadline = time.monotonic() + args.max_wait_seconds
    last_state = None
    printed_result = False
    while True:
        data = api_json(Request(detail_url, headers=common_headers))["data"]
        state = (data.get("status"), data.get("execution_status"), data.get("cleanup_complete"))
        if state != last_state:
            print(json.dumps({"status": state[0], "execution_status": state[1], "cleanup_complete": state[2]}, ensure_ascii=False), flush=True)
            last_state = state
        if data.get("execution_status") in TERMINAL and not printed_result:
            print(json.dumps({
                "submission_id": str(submission_id), "execution_status": data["execution_status"],
                "exit_code": data.get("exit_code"), "output": data.get("output"),
            }, ensure_ascii=False))
            printed_result = True
        if printed_result and data.get("cleanup_complete") is True:
            return 0
        if time.monotonic() >= deadline:
            print("Timed out waiting for RESULT and CLEANUP; use submission_id to inspect later.", file=sys.stderr)
            return 2
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
