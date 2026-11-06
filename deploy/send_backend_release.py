#!/usr/bin/env python3
"""Send only a release identity to the pinned, fixed backend deployment entry point."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from backend_release import CENTRAL_REVISION, PREFIX, manifest

ERROR_CODES = {"BASE_TEMPLATE_CHANGED", "RANKING_SCOPE_FAILED", "HELPER_CHANGED", "MIGRATION_OR_SECURITY_CHECK_FAILED", "UNVERIFIED_RELEASE"}


class DeploymentFailure(RuntimeError):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "UNVERIFIED_RELEASE"
        super().__init__(self.code)


def scope_proof(value):
    if not isinstance(value, dict) or value.get("track") != "external" or value.get("division") != "EXTERNAL":
        raise DeploymentFailure("RANKING_SCOPE_FAILED")
    if (type(value.get("team_count")) is not int or value["team_count"] != 50
            or type(value.get("configured_roster_count")) is not int or value["configured_roster_count"] != 50
            or not isinstance(value.get("roster_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["roster_sha256"])
            or value.get("api_verified") is not True or value.get("database_probe_read_only") is not True):
        raise DeploymentFailure("RANKING_SCOPE_FAILED")
    return {key: value[key] for key in ["track", "division", "team_count", "configured_roster_count", "roster_sha256", "api_verified", "database_probe_read_only"]}


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
