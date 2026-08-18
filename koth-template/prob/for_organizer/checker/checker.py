import json
import os
import sys
from datetime import datetime, timezone
import urllib.error
import urllib.request


def result(team_id, koth_challenge_id, score):
    print(json.dumps({
        "team_id": team_id,
        "koth_challenge_id": koth_challenge_id,
        "metric_score": score,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }))


def main():
    host = os.environ.get("TARGET_HOST")
    port = os.environ.get("TARGET_PORT")
    # team_id와 koth_challenge_id는 UUID 문자열이므로 정수로 변환하지 않는다.
    team_id = os.environ.get("TEAM_ID")
    koth_challenge_id = os.environ.get("KOTH_CHALLENGE_ID")

    if not host or not port or not team_id or not koth_challenge_id:
        result(team_id, koth_challenge_id, 0)
        return

    url = f"http://{host}:{port}/"

    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status != 200:
                result(team_id, koth_challenge_id, 0)
                return
    except urllib.error.URLError as exc:
        print(f"service unreachable: {exc}", file=sys.stderr)
        result(team_id, koth_challenge_id, 0)
        return
    except TimeoutError:
        print("service timeout", file=sys.stderr)
        result(team_id, koth_challenge_id, 0)
        return

    # TODO: replace this with the real KOTH condition check.
    result(team_id, koth_challenge_id, 100)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"checker error: {exc}", file=sys.stderr)
        result(os.environ.get("TEAM_ID"), os.environ.get("KOTH_CHALLENGE_ID"), 0)
        sys.exit(1)
