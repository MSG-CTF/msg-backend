import json
import sys
import time
import urllib.request
from pathlib import Path

url = sys.argv[1]
limit = float(sys.argv[2])
output = Path(sys.argv[3])
started = time.perf_counter()
attempts = 0
recovered = False
last_error = None

while time.perf_counter() - started < limit:
    attempts += 1
    try:
        request = urllib.request.Request(url, headers={"X-Forwarded-Proto": "https"})
        with urllib.request.urlopen(request, timeout=1) as response:
            recovered = response.status == 200
        if recovered:
            break
    except Exception as exc:
        last_error = type(exc).__name__
    time.sleep(0.5)

result = {
    "recovered": recovered,
    "recovery_seconds": time.perf_counter() - started,
    "attempts": attempts,
    "last_error": last_error,
}
output.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
