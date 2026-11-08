#!/usr/bin/env python3
"""Send only a release identity to the pinned, fixed backend deployment entry point."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import uuid

from backend_release import CENTRAL_REVISION, PREFIX, manifest

ERROR_CODES = {"BASE_TEMPLATE_CHANGED", "RANKING_SCOPE_FAILED", "HELPER_CHANGED", "MIGRATION_OR_SECURITY_CHECK_FAILED", "UNVERIFIED_RELEASE"}


class DeploymentFailure(RuntimeError):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "UNVERIFIED_RELEASE"
        super().__init__(self.code)


def scope_proof(value):
    if not isinstance(value, dict) or value.get("track") != "external" or value.get("division") != "EXTERNAL":
        raise DeploymentFailure("RANKING_SCOPE_FAILED")
    banned = value.get("banned_official_team_ids", [])
    if (not isinstance(banned, list) or any(not isinstance(item, str) for item in banned)
            or len(set(banned)) != len(banned) or len(banned) >= 50):
        raise DeploymentFailure("RANKING_SCOPE_FAILED")
    try:
        if any(str(uuid.UUID(item)) != item for item in banned):
            raise ValueError("Noncanonical official identity")
    except ValueError:
        raise DeploymentFailure("RANKING_SCOPE_FAILED") from None
    official_digest = value.get("official_roster_sha256")
    if ((banned or official_digest is not None)
            and (not isinstance(official_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", official_digest))):
        raise DeploymentFailure("RANKING_SCOPE_FAILED")
    if (type(value.get("team_count")) is not int or value["team_count"] != 50 - len(banned)
            or type(value.get("configured_roster_count")) is not int or value["configured_roster_count"] != 50
            or not isinstance(value.get("roster_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["roster_sha256"])
            or value.get("api_verified") is not True or value.get("database_probe_read_only") is not True):
        raise DeploymentFailure("RANKING_SCOPE_FAILED")
    result = {key: value[key] for key in ["track", "division", "team_count", "configured_roster_count", "roster_sha256", "api_verified", "database_probe_read_only"]}
    if "official_roster_sha256" in value:
        result["official_roster_sha256"] = official_digest
    if "banned_official_team_ids" in value:
        result["banned_official_team_ids"] = banned
    return result


def send(action, environment=None):
    environment = dict(os.environ if environment is None else environment)
    request = manifest(
        json.dumps(
            {
                "action": action,
                "source_sha": environment["SOURCE_SHA"],
                "deployment_id": environment["DEPLOYMENT_ID"],
                "pipeline_revision": CENTRAL_REVISION,
            }
        ).encode()
    )
    host = environment["BACKEND_DEPLOY_HOST"]
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        raise ValueError("Invalid deployment host")
    transport = environment.get("BACKEND_DEPLOY_TRANSPORT", "direct")
    if transport not in {"direct", "broker"}:
        raise ValueError("Invalid deployment transport")
    if transport == "broker":
        if host not in {"msgctf.kr", "136.108.97.182"}:
            raise ValueError("Broker destination must be the existing external VM")
        if not all(environment.get(name, "").strip() for name in [
            "BACKEND_DEPLOY_BROKER_SSH_KEY", "BACKEND_DEPLOY_BROKER_KNOWN_HOSTS"
        ]):
            raise ValueError("Broker credentials are required; direct fallback is disabled")
    with tempfile.TemporaryDirectory(prefix="backend-release-") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        key, hosts = directory / "key", directory / "known_hosts"
        for path, field in [
            (key, "BACKEND_DEPLOY_SSH_KEY"),
            (hosts, "BACKEND_DEPLOY_KNOWN_HOSTS"),
        ]:
            path.write_text(environment[field].rstrip() + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        broker_options = []
        if transport == "broker":
            broker_key, broker_hosts = directory / "broker_key", directory / "broker_hosts"
            for path, field in [
                (broker_key, "BACKEND_DEPLOY_BROKER_SSH_KEY"),
                (broker_hosts, "BACKEND_DEPLOY_BROKER_KNOWN_HOSTS"),
            ]:
                path.write_text(environment[field].rstrip() + "\n", encoding="utf-8")
                os.chmod(path, 0o600)
            # This existing forced command is a fixed TCP hop, independent of the
            # separate frontend/backend account authenticated on the destination.
            proxy = [
                "ssh", "-F", "/dev/null", "-T", "-i", str(broker_key),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile=" + str(broker_hosts),
                "-o", "HostKeyAlias=broker.mjsec.kr",
                "-o", "ConnectTimeout=10", "-o", "ForwardAgent=no",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                "sla@35.193.235.206", "proxy-frontend-v1",
            ]
            broker_options = ["-o", "ProxyCommand=" + shlex.join(proxy)]
        clean = {
            k: v for k, v in environment.items() if not k.startswith("BACKEND_DEPLOY_")
        }
        result = subprocess.run(
            [
                "ssh",
                "-T",
                "-i",
                str(key),
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + str(hosts),
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=4",
                "-o",
                "ForwardAgent=no",
                *broker_options,
                "msg-backend-deploy@" + host,
                "deploy",
            ],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            env=clean,
            timeout=960,
        )
        reports = [
            json.loads(line[len(PREFIX) :])
            for line in result.stdout.splitlines()
            if line.startswith(PREFIX)
        ]
        if result.returncode:
            for line in result.stderr.splitlines():
                if line.startswith("PLATFORM_DEPLOY_ERROR="):
                    try:
                        code = json.loads(line.split("=", 1)[1]).get("error_code")
                    except (ValueError, TypeError, AttributeError):
                        continue
                    raise DeploymentFailure(code)
            raise DeploymentFailure("UNVERIFIED_RELEASE")
        expected = "preflight_verified" if action == "probe" else "verified"
        superseded = action == "deploy" and len(reports) == 1 and reports[0].get("status") == "superseded"
        if (
            len(reports) != 1
            or (reports[0].get("status") != expected and not superseded)
            or reports[0].get("source_commit") != request["source_sha"]
            or reports[0].get("pipeline_revision") != CENTRAL_REVISION
        ):
            raise DeploymentFailure("UNVERIFIED_RELEASE")
        proof = {
            k: reports[0][k]
            for k in [
                "source_commit",
                "pipeline_revision",
                "deployment_strategy",
                "status",
            ]
        }
        if superseded:
            current = reports[0].get("current_main_source_commit")
            if not isinstance(current, str) or not re.fullmatch(r"[a-f0-9]{40}", current) or current == request["source_sha"]:
                raise DeploymentFailure("UNVERIFIED_RELEASE")
            return {**proof, "current_main_source_commit": current}
        return {**proof, "ranking_scope": scope_proof(reports[0].get("ranking_scope"))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["probe", "deploy"])
    args = parser.parse_args()
    print(PREFIX + json.dumps(send(args.action)))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = error.code if isinstance(error, DeploymentFailure) else "UNVERIFIED_RELEASE"
        raise SystemExit("Restricted backend release rejected or unverified (" + code + ")") from None
