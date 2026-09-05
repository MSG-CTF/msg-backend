import io
import json
import logging
import uuid
import zipfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction

from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRelease
from apps.instances.releases import (
    ReleaseValidationError,
    check_slug_consistency,
    create_release,
    validate_release_payload,
)

logger = logging.getLogger(__name__)

BUNDLE_NAME_SUFFIX = "-publish-bundle"
BUNDLE_FILE_NAME = "artifact-v2.json"
POLLER_CREATED_BY = "release-poller"


def github_request(path, token=None, timeout=10):
    # 공급망 artifact 조회용 GitHub API 호출. 응답 본문 bytes를 돌려준다
    base = settings.RELEASE_POLL_API_BASE.rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ReleaseValidationError("RELEASE_POLL_API_BASE는 http 또는 https 주소여야 합니다")

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "msg-backend-release-poller",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(base + path, headers=headers)
    # 위에서 scheme을 http와 https로 제한한 운영 설정만 사용한다
    with urlopen(request, timeout=timeout) as response:  # nosec B310
        return response.read()


def list_bundle_artifacts(token=None):
    # 문제 저장소의 Actions artifact 중 publish bundle만 최신순으로 돌려준다
    raw = github_request(
        "/repos/" + settings.RELEASE_POLL_REPO
        + "/actions/artifacts?per_page=" + str(settings.RELEASE_POLL_LIMIT),
        token=token,
    )
    artifacts = json.loads(raw.decode("utf-8")).get("artifacts", [])
    return [
        entry
        for entry in artifacts
        if entry.get("name", "").endswith(BUNDLE_NAME_SUFFIX)
        and not entry.get("expired")
    ]


def workflow_run_id(artifact):
    workflow_run = artifact.get("workflow_run")
    if not isinstance(workflow_run, dict):
        raise ReleaseValidationError("artifact workflow_run 값이 올바르지 않습니다")

    run_id = workflow_run.get("id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise ReleaseValidationError("artifact workflow_run.id 값이 올바르지 않습니다")
    return run_id


def get_workflow_run(artifact, token=None):
    raw = github_request(
        "/repos/" + settings.RELEASE_POLL_REPO
        + "/actions/runs/" + str(workflow_run_id(artifact)),
        token=token,
    )
    workflow_run = json.loads(raw.decode("utf-8"))
    if not isinstance(workflow_run, dict):
        raise ReleaseValidationError("workflow_run 응답 값이 올바르지 않습니다")
    return workflow_run


def download_bundle(artifact, token=None):
    # bundle zip을 내려받아 artifact-v2.json 내용을 dict로 돌려준다
    raw = github_request(
        "/repos/" + settings.RELEASE_POLL_REPO
        + "/actions/artifacts/" + str(artifact["id"]) + "/zip",
        token=token,
        timeout=30,
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name in archive.namelist():
            if name.split("/")[-1] == BUNDLE_FILE_NAME:
                return json.loads(archive.read(name).decode("utf-8"))
    raise ReleaseValidationError("bundle 안에 " + BUNDLE_FILE_NAME + " 파일이 없습니다")


def expected_source_ref():
    value = getattr(settings, "RELEASE_POLL_SOURCE_REF", "refs/heads/main")
    return str(value or "").strip()


def branch_from_source_ref(source_ref):
    prefix = "refs/heads/"
    if not source_ref.startswith(prefix):
        raise ReleaseValidationError("RELEASE_POLL_SOURCE_REF는 refs/heads/* 형식이어야 합니다")
    return source_ref[len(prefix):]


def source_sha(artifact_data):
    value = artifact_data.get("source_sha") or artifact_data.get("commit_sha")
    if not isinstance(value, str) or not value.strip():
        raise ReleaseValidationError("bundle source_sha 값이 올바르지 않습니다")
    return value.strip()


def validate_workflow_run_source(artifact, workflow_run):
    expected_ref = expected_source_ref()
    expected_branch = branch_from_source_ref(expected_ref)
    artifact_run = artifact.get("workflow_run") or {}

    if workflow_run.get("id") != workflow_run_id(artifact):
        raise ReleaseValidationError("artifact와 workflow_run id가 일치하지 않습니다")
    if workflow_run.get("status") != "completed" or workflow_run.get("conclusion") != "success":
        raise ReleaseValidationError("성공한 workflow_run의 bundle만 등록할 수 있습니다")
    if workflow_run.get("head_branch") != expected_branch:
        raise ReleaseValidationError("workflow_run branch가 등록 허용 branch와 일치하지 않습니다")
    if artifact_run.get("head_branch") and artifact_run.get("head_branch") != workflow_run.get("head_branch"):
        raise ReleaseValidationError("artifact와 workflow_run branch가 일치하지 않습니다")
    if artifact_run.get("head_sha") and artifact_run.get("head_sha") != workflow_run.get("head_sha"):
        raise ReleaseValidationError("artifact와 workflow_run sha가 일치하지 않습니다")


def validate_bundle_source(artifact, workflow_run, artifact_data):
    validate_workflow_run_source(artifact, workflow_run)
    expected_ref = expected_source_ref()
    if artifact_data.get("source_ref") != expected_ref:
        raise ReleaseValidationError("bundle source_ref가 등록 허용 ref와 일치하지 않습니다")
    if source_sha(artifact_data) != workflow_run.get("head_sha"):
        raise ReleaseValidationError("bundle source_sha와 workflow_run sha가 일치하지 않습니다")


def match_challenge(artifact_data):
    challenge_id = artifact_data.get("challenge_id")
    if challenge_id:
        try:
            uuid.UUID(str(challenge_id))
        except (TypeError, ValueError):
            return None

        return Challenge.objects.filter(challenge_id=challenge_id).first()

    slug = artifact_data.get("challenge_slug", "")
    release = (
        ChallengeRelease.objects
        .filter(challenge_slug=slug)
        .select_related("challenge")
        .first()
    )
    if release is not None:
        return release.challenge

    return None


def register_bundle(artifact_data, note=None):
    # bundle 한 벌을 검증해 릴리스로 등록한다. 결과는 (상태, 상세) 튜플이다
    try:
        validated = validate_release_payload({"artifact": artifact_data, "note": note})
    except ReleaseValidationError as error:
        return "invalid", error.message

    challenge = match_challenge(artifact_data)
    if challenge is None:
        return "unmatched", artifact_data.get("challenge_slug", "")

    with transaction.atomic():
        challenge = Challenge.objects.select_for_update().get(pk=challenge.pk)

        duplicated = ChallengeRelease.objects.filter(
            challenge=challenge,
            registry_revision=validated["registry_revision"],
        ).exists()
        if duplicated:
            return "duplicate", validated["registry_revision"]

        try:
            check_slug_consistency(challenge, validated["challenge_slug"])
        except ReleaseValidationError as error:
            return "invalid", error.message

        release = create_release(challenge, validated, POLLER_CREATED_BY)

    return "registered", release


def poll_once(token=None):
    # 공급망 bundle을 한 바퀴 수집해 새 릴리스만 등록한다. 전환은 하지 않는다
    summary = {"registered": 0, "duplicate": 0, "unmatched": 0, "invalid": 0, "error": 0}
    try:
        artifacts = list_bundle_artifacts(token=token)
    except ReleaseValidationError as error:
        logger.warning("release poller 설정 오류: %s", error.message)
        summary["error"] += 1
        return summary
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        logger.warning("release poller artifact 목록 조회 실패: %s", error)
        summary["error"] += 1
        return summary

    for artifact in artifacts:
        try:
            workflow_run = get_workflow_run(artifact, token=token)
            validate_workflow_run_source(artifact, workflow_run)
            artifact_data = download_bundle(artifact, token=token)
        except (HTTPError, URLError, TimeoutError, ValueError, zipfile.BadZipFile) as error:
            logger.warning("release poller bundle 다운로드 실패 %s: %s", artifact.get("name"), error)
            summary["error"] += 1
            continue
        except ReleaseValidationError as error:
            logger.warning("release poller bundle 형식 오류 %s: %s", artifact.get("name"), error.message)
            summary["invalid"] += 1
            continue

        try:
            validate_bundle_source(artifact, workflow_run, artifact_data)
        except ReleaseValidationError as error:
            logger.warning("release poller bundle 출처 오류 %s: %s", artifact.get("name"), error.message)
            summary["invalid"] += 1
            continue

        status, detail = register_bundle(
            artifact_data,
            note="공급망 자동 등록: " + str(artifact.get("name", "")),
        )
        summary[status] += 1
        if status == "registered":
            logger.info(
                "release poller 등록: challenge=%s version=%s revision=%s",
                detail.challenge_id,
                detail.version,
                detail.registry_revision,
            )
        elif status in ("unmatched", "invalid"):
            logger.warning("release poller 건너뜀 (%s): %s", status, detail)

    return summary
