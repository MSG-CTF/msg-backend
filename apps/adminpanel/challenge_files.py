"""관리자용 참가자 ZIP 조회와 파일 풀이 문제의 ZIP 고정. 원본 저장소 목록과 배포 파일을 구분한다."""
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse
from django.views.decorators.cache import never_cache
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from rest_framework.decorators import api_view, permission_classes

from apps.adminpanel.models import AdminEvent
from apps.challenge.models import Challenge
from apps.common.idempotency import run_idempotent
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig, ChallengeUserFileBundle, ChallengeUserFileSelection
from apps.instances.release_files import file_bundle_for_release, file_bundle_metadata, release_files_summary
from apps.instances.user_files import static_user_file_choice, USER_FILES_ARCHIVE


def _isoformat(value):
    return value.isoformat().replace("+00:00", "Z")


def _file_choice_label(bundle, selection):
    if bundle is None:
        return "없음"
    return f"발행 #{bundle.registry_revision} 고정" if selection else f"최신 자동 발행 #{bundle.registry_revision}"


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def challenge_files(request, challenge_id):
    challenge = Challenge.objects.select_related("runtime_config__current_release").filter(pk=challenge_id).first()
    if challenge is None:
        return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)
    config = getattr(challenge, "runtime_config", None)
    release = config.current_release if config else None
    requested_release_id = request.query_params.get("release_id")
    if requested_release_id is not None:
        try:
            requested_release_id = UUID(requested_release_id)
        except ValueError:
            return fail("INVALID_REQUEST", "배포 버전 ID를 확인해주세요", 400)
        release = ChallengeRelease.objects.filter(challenge=challenge, pk=requested_release_id).first()
        if release is None:
            return fail("RELEASE_NOT_FOUND", "존재하지 않는 배포 버전입니다", 404)
    # 파일 풀이 문제는 고정한 ZIP이 있으면 그것을, 없으면 가장 높은 발행번호를 참가자에게 준다
    selection = None
    if release:
        bundle = file_bundle_for_release(release)
    elif config:
        bundle = None
    else:
        bundle, selection = static_user_file_choice(challenge)
    if release:
        status = release_files_summary(release)["status"]
    elif config:
        status = "no_release"
    elif bundle is None:
        status = "pending"
    elif not bundle.present:
        status = "none"
    elif not bundle.object_key:
        status = "unavailable"
    else:
        status = "available"
    try:
        page = int(request.query_params.get("page", "1"))
        if not 1 <= page <= 100000:
            raise ValueError
    except (ValueError, TypeError):
        return fail("INVALID_REQUEST", "파일 목록 페이지를 확인해주세요", 400)
    history_query = challenge.user_file_bundles.order_by("-registry_revision")
    if bundle:
        history_query = history_query.exclude(pk=bundle.pk)
    offset = (page - 1) * 20
    history = list(history_query[offset:offset + 21])
    matching = (challenge.user_file_bundles.filter(registry_revision=release.registry_revision).first()
                if release else None)
    latest = challenge.user_file_bundles.order_by("-registry_revision", "-created_at").first()
    storage = settings.STORAGES["default"]
    is_gcs = storage["BACKEND"] == "storages.backends.gcloud.GoogleCloudStorage"
    return ok({
        "status": status,
        "release_version": release.version if release else None,
        "registry_revision": release.registry_revision if release else (bundle.registry_revision if bundle else None),
        "release_id": str(release.pk) if release else None,
        "is_current": bool(release and config and config.current_release_id == release.pk),
        "mode": release.user_files_mode if release else ("SELECTED" if selection else "LATEST"),
        "selected_by": selection.selected_by if selection else None,
        "selected_at": _isoformat(selection.updated_at) if selection else None,
        "current_file": file_bundle_metadata(bundle),
        "latest_file": file_bundle_metadata(latest),
        "matching_file": file_bundle_metadata(matching),
        "history": [file_bundle_metadata(item) for item in history[:20]],
        "history_has_more": len(history) > 20,
        "history_next_page": page + 1 if len(history) > 20 else None,
        "storage": {"provider": "gcs" if is_gcs else "local",
                    "bucket": storage.get("OPTIONS", {}).get("bucket_name", getattr(settings, "GS_BUCKET_NAME", "")) if is_gcs else None},
    })


@never_cache
@api_view(["POST"])
@permission_classes([IsAdmin])
def challenge_file_selection(request, challenge_id):
    """실행 이미지가 없는 파일 풀이 문제에서 참가자에게 줄 ZIP을 고정하거나 최신 자동으로 돌린다."""
    body = request.data
    if not isinstance(body, dict) or set(body) - {"mode", "file_id", "reason"}:
        return fail("FILE_SELECTION_INVALID", "파일 설정 형식을 확인해주세요", 400)
    mode = body.get("mode")
    if mode not in ("LATEST", "SELECTED") or (mode == "LATEST" and "file_id" in body):
        return fail("FILE_SELECTION_INVALID", "파일 제공 방식을 확인해주세요", 400)
    reason = body.get("reason")
    if reason is not None and (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500):
        return fail("FILE_SELECTION_INVALID", "사유는 1~500자로 적어주세요", 400)
    file_id = None
    if mode == "SELECTED":
        try:
            file_id = UUID(body["file_id"]) if isinstance(body.get("file_id"), str) else None
        except ValueError:
            pass
        if file_id is None:
            return fail("FILE_SELECTION_INVALID", "고정할 ZIP을 선택해주세요", 400)
    with transaction.atomic():
        challenge = Challenge.objects.select_for_update().filter(pk=challenge_id).first()
        if challenge is None:
            return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다", 404)
        if ChallengeRuntimeConfig.objects.filter(challenge=challenge).exists():
            return fail("RUNTIME_CHALLENGE_FILES", "실행 문제는 배포 버전에서 참가자 파일을 고릅니다", 409)
        bundle = None
        if file_id:
            bundle = ChallengeUserFileBundle.objects.filter(challenge=challenge, pk=file_id, present=True).first()
            if bundle is None or not bundle.object_key:
                return fail("FILE_NOT_FOUND", "이 문제에 수집된 ZIP을 선택해주세요", 404)

        def save():
            before, previous = static_user_file_choice(challenge)
            if (previous.bundle_id if previous else None) == (bundle.pk if bundle else None):
                return {"mode": mode, "current_file": file_bundle_metadata(before)}
            if bundle is None:
                previous.delete()
            else:
                ChallengeUserFileSelection.objects.update_or_create(
                    challenge=challenge, defaults={"bundle": bundle, "selected_by": request.user.login_id})
            after, selection = static_user_file_choice(challenge)
            latest = challenge.user_file_bundles.order_by("-registry_revision").first()
            # 더 높은 발행번호를 두고 예전 ZIP을 주는 것은 되돌림이라 경고로 남긴다
            rollback = bool(selection and latest and after.registry_revision < latest.registry_revision)
            message = (f"참가자 파일 {'고정' if selection else '최신 자동'}: "
                       f"{_file_choice_label(before, previous)} → {_file_choice_label(after, selection)}")
            if reason:
                message += f": {reason.strip()}"
            AdminEvent.objects.create(
                type=AdminEvent.EventType.CHALLENGE_UPDATED,
                severity=AdminEvent.Severity.WARNING if rollback else AdminEvent.Severity.INFO,
                message=message, challenge=challenge, actor=request.user.login_id,
            )
            return {"mode": mode, "current_file": file_bundle_metadata(after)}

        return run_idempotent(request, body, save, message="참가자 파일 설정을 저장했습니다")


@never_cache
@api_view(["GET"])
@permission_classes([IsAdmin])
def challenge_file_download(request, challenge_id, file_id):
    bundle = ChallengeUserFileBundle.objects.filter(pk=file_id, challenge_id=challenge_id, present=True).first()
    if bundle is None or not bundle.object_key:
        return fail("FILE_NOT_FOUND", "등록된 참가자 파일이 없습니다", 404)
    try:
        if not default_storage.exists(bundle.object_key):
            return fail("FILE_NOT_FOUND", "저장소에서 ZIP 파일을 찾지 못했습니다", 404)
        archive = default_storage.open(bundle.object_key, "rb")
        return FileResponse(archive, as_attachment=True, filename=USER_FILES_ARCHIVE, content_type="application/zip")
    except FileNotFoundError:
        return fail("FILE_NOT_FOUND", "저장소에서 ZIP 파일을 찾지 못했습니다", 404)
    except (OSError, GoogleAPIError, GoogleAuthError, ImproperlyConfigured):
        return fail("FILE_STORAGE_UNAVAILABLE", "파일 저장소에 연결하지 못했습니다 인증과 읽기 권한을 확인해주세요", 503)
