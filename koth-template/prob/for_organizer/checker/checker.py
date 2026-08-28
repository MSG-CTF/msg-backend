import json
import ipaddress
import os
import re
import sys
from datetime import datetime, timezone
import urllib.error
import urllib.request


DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?$")


def result(team_id, koth_challenge_id, score):
    print(json.dumps({
        "team_id": team_id,
        "koth_challenge_id": koth_challenge_id,
        "metric_score": score,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }))


def target_url(host, port):
    try:
        address = ipaddress.ip_address(host)
        safe_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    except ValueError:
        hostname = host.rstrip(".")
        labels = hostname.split(".")
        if len(hostname) > 253 or not all(DNS_LABEL.fullmatch(label) for label in labels):
            return None
        safe_host = hostname

    try:
        safe_port = int(port)
    except ValueError:
        return None

    if not 1 <= safe_port <= 65535:
        return None

    return f"http://{safe_host}:{safe_port}/"


def main():
    host = os.environ.get("TARGET_HOST")
    port = os.environ.get("TARGET_PORT")
    # team_id와 koth_challenge_id는 UUID 문자열이므로 정수로 변환하지 않는다.
    team_id = os.environ.get("TEAM_ID")
    koth_challenge_id = os.environ.get("KOTH_CHALLENGE_ID")

    if not host or not port or not team_id or not koth_challenge_id:
        result(team_id, koth_challenge_id, 0)
        return

    url = target_url(host, port)
    if url is None:
        result(team_id, koth_challenge_id, 0)
        return

    try:
        # scheme은 http로 고정되고 host와 port는 target_url에서 검증된다.
        with urllib.request.urlopen(url, timeout=5) as response:  # nosec B310
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
