"""Administrator-only atomic installation of reviewed CD code and a site roster."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

try:
    from .ranking_scope import SITES, validate_scope
except ImportError:
    from ranking_scope import SITES, validate_scope


def atomic_bytes(path, value, mode=0o600):
    fd, name = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def persisted_environment(raw, scope):
    # Retain every unrelated byte, including all existing credential values.
    keys = (b"PUBLIC_RANKING_DIVISION=", b"PUBLIC_RANKING_TEAM_IDS=")
    retained = b"".join(line for line in raw.splitlines(keepends=True) if not line.startswith(keys))
    if retained and not retained.endswith(b"\n"):
        retained += b"\n"
    return retained + ("PUBLIC_RANKING_DIVISION=" + scope["division"] + "\nPUBLIC_RANKING_TEAM_IDS=" + ",".join(scope["team_ids"]) + "\n").encode()


def install(policy_path, scope_value, source_sha, expected_policy_sha256):
    if os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Reviewed administrator installation required")
    policy_path = Path(policy_path)
    metadata = policy_path.lstat()
    if policy_path.resolve() != policy_path or metadata.st_uid != 0 or metadata.st_mode & 0o022 or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Untrusted host policy")
    original = policy_path.read_bytes()
    if hashlib.sha256(original).hexdigest() != expected_policy_sha256:
        raise ValueError("Host policy changed after review")
    policy = json.loads(original)
    scope = validate_scope(scope_value, policy["track"])
    if policy["domain"] != SITES[scope["track"]][2] or policy["root"] != "/opt/ctf-" + scope["track"]:
        raise ValueError("Unexpected deployment destination")
    runtime = Path(policy["root"]) / "cd"
    runtime.mkdir(mode=0o700, exist_ok=True)
    target = Path("/usr/local/lib/ctf-platform-cd")
    target.mkdir(mode=0o755, exist_ok=True)
    if target.resolve() != target or target.stat().st_uid != 0 or target.stat().st_mode & 0o022:
        raise ValueError("Untrusted helper directory")
    source = Path(__file__).resolve().parent
    files = ["platform_release.py", "ranking_scope.py"]
    if scope["track"] == "external":
        files.append("legacy_entry.py")
    scope_path = policy_path.parent / "ranking-scope.json"
    scope_raw = (json.dumps(scope, sort_keys=True, indent=2) + "\n").encode()
    env_name = "public-runtime.env" if scope["track"] == "external" else "runtime.env"
    env_path = Path("/etc/ctf-" + scope["track"] + "-secrets/app") / env_name
    env_meta = env_path.lstat()
    if env_path.resolve() != env_path or env_meta.st_uid != 0 or env_meta.st_mode & 0o077 or not stat.S_ISREG(env_meta.st_mode):
        raise ValueError("Untrusted existing runtime environment")
    with contextlib.ExitStack() as stack:
        for name in ["build.lock", "deployment.lock"]:
            lock = stack.enter_context((runtime / name).open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX)
        if policy_path.read_bytes() != original:
            raise ValueError("Host policy changed while waiting for installation")
        for name, expected in policy.get("trusted_helper_sha256", {}).items():
            existing = target / name
            if existing.is_symlink() or existing.stat().st_uid != 0 or existing.stat().st_mode & 0o022 or hashlib.sha256(existing.read_bytes()).hexdigest() != expected:
                raise ValueError("Existing helper differs from reviewed policy")
        for name, expected in policy["base_file_sha256"].items():
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
                raise ValueError("Pinned deployment template changed; no automatic approval")
        backup = runtime / ("guard-install-" + source_sha[:12])
        backup.mkdir(mode=0o700)
        changes = {scope_path: scope_raw, env_path: persisted_environment(env_path.read_bytes(), scope)}
        for name in files:
            changes[target / name] = (source / name).read_bytes()
        policy["ranking_scope_file"] = str(scope_path)
        policy["ranking_scope_sha256"] = hashlib.sha256(scope_raw).hexdigest()
        policy["managed_helper_source_sha"] = source_sha
        for name in files:
            policy.setdefault("trusted_helper_sha256", {})[name] = hashlib.sha256(changes[target / name]).hexdigest()
        changes[policy_path] = (json.dumps(policy, indent=2) + "\n").encode()
        originals = {}
        for index, path in enumerate(changes):
            originals[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
            if path.exists():
                atomic_bytes(backup / (str(index) + ".before"), originals[path][0])
        atomic_bytes(backup / "paths.json", (json.dumps([str(path) for path in changes], indent=2) + "\n").encode())
        try:
            # Scope and code first; policy is the final commit point under both locks.
            for path, raw in changes.items():
                atomic_bytes(path, raw, 0o755 if path.name == "legacy_entry.py" else 0o600)
        except BaseException:
            for path, old in originals.items():
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_bytes(path, old[0], old[1])
            raise
    return {"track": scope["track"], "team_count": len(scope["team_ids"]), "source_sha": source_sha,
            "template_checksums_preserved": True, "runtime_environment_keys_managed": ["PUBLIC_RANKING_DIVISION", "PUBLIC_RANKING_TEAM_IDS"],
            "application_restarted": False, "database_accessed": False, "backup": str(backup)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--expected-policy-sha256", required=True)
    args = parser.parse_args()
    result = install("/etc/ctf-platform-cd/config.json", json.loads(args.scope.read_text()), args.source_sha, args.expected_policy_sha256)
    print(json.dumps(result))
