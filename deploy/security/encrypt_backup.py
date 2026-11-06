#!/usr/bin/env python3
"""Encrypt an incoming compressed dump with an offline recovery recipient."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def encrypt(source, destination, recipients):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink() or not destination.name.endswith(".age"):
        raise ValueError("A new encrypted backup path is required")
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".encrypted-backup-", dir=destination.parent)
    os.close(descriptor)
    process = None
    try:
        process = subprocess.Popen(["age", "--recipients-file", str(recipients), "--output", temporary],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        digest = hashlib.sha256()
        size = 0
        while block := source.read(1024 * 1024):
            digest.update(block)
            size += len(block)
            process.stdin.write(block)
        process.stdin.close()
        process.wait(timeout=120)
        if process.returncode or size == 0:
            raise ValueError("Backup encryption failed")
        with open(temporary, "rb") as output:
            if output.readline() != b"age-encryption.org/v1\n":
                raise ValueError("Invalid encrypted backup header")
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        # link() avoids clobbering an existing backup created concurrently.
        os.link(temporary, destination)
        metadata = {"format":"age-v1", "compressed_plaintext_sha256":digest.hexdigest(),
            "compressed_plaintext_bytes":size,
            "created_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}
        metadata_path = destination.with_name(destination.name + ".metadata.json")
        with metadata_path.open("x") as output:
            json.dump(metadata, output)
        os.chmod(metadata_path, 0o600)
        return metadata
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--recipients", type=Path, default=Path("/etc/msgctf-security/backup-recipients.txt"))
    args = parser.parse_args()
    os.umask(0o077)
    if args.source:
        with args.source.open("rb") as source:
            encrypt(source, args.destination, args.recipients)
    else:
        encrypt(sys.stdin.buffer, args.destination, args.recipients)


if __name__ == "__main__":
    main()
