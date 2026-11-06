#!/usr/bin/env python3
"""Hydrate a systemd encrypted credential into the application's tmpfs mount."""
import json
import os
from pathlib import Path
import stat
import tempfile

ALLOWED = {"DJANGO_SECRET_KEY", "JWT_SECRET", "KOTH_TEAM_TOKEN_SECRET",
    "RUNTIME_SECRET_ENCRYPTION_KEYS", "RUNTIME_SECRET_API_TOKEN",
    "SCHEDULER_API_TOKEN", "EXE_CHECKER_SIGNING_KEY"}


def hydrate(payload, destination, group_id):
    values = payload.get("secrets", {})
    if payload.get("schema") != 1 or set(values) != ALLOWED:
        raise ValueError("Unexpected service secret schema")
    if any(not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value or "\x00" in value for value in values.values()):
        raise ValueError("Invalid service secret field")
    parent = destination.parent
    parent.mkdir(mode=0o710, parents=True, exist_ok=True)
    os.chown(parent, 0, group_id)
    # Only root can list/write; the application group may traverse to its file.
    os.chmod(parent, stat.S_IRWXU | stat.S_IXGRP)
    descriptor, temporary = tempfile.mkstemp(prefix=".service-keys-", dir=parent)
    try:
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, 0, group_id)
        with os.fdopen(descriptor, "w") as output:
            output.write("".join(name + "=" + json.dumps(value) + "\n" for name, value in sorted(values.items())))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    os.umask(0o077)
    credential_path = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "backend.env"
    metadata = credential_path.lstat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o022 or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Untrusted systemd credential")
    payload = json.loads(credential_path.read_text())
    destination = Path("/run/msgctf-secrets/backend.env")
    hydrate(payload, destination, 101)
    print(json.dumps({"status":"synced", "key_roles":len(payload["secrets"])}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Service key synchronization failed; no secret values logged") from None
