from django.db import transaction
from django.utils import timezone
from rest_framework.views import APIView

from apps.challenge.models import Challenge
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig
from apps.instances.releases import (
    ReleaseValidationError,
    check_slug_consistency,
    create_release,
    is_deployable,
    serialize_release,
    validate_release_payload,
)
from apps.instances.services import isoformat_z


def _get_challenge(challenge_id, for_update=False):
    queryset = Challenge.objects.all()
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.filter(challenge_id=challenge_id).first()


def _current_release_id(challenge):
    config = ChallengeRuntimeConfig.objects.filter(challenge=challenge).first()
    if config is None:
        return None
    return config.current_release_id


class ReleaseListCreateView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request, challenge_id):
        # 문제의 릴리스 이력을 최신 버전부터 반환한다
        challenge = _get_challenge(challenge_id)
        if challenge is None:
            return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

        current_id = _current_release_id(challenge)
        releases = (
            ChallengeRelease.objects
            .filter(challenge=challenge)
            .prefetch_related("containers")
            .order_by("-version")
        )
        rows = [serialize_release(release, current_id) for release in releases]

        return ok(
            {
                "challenge_id": str(challenge.challenge_id),
                "current_release_id": str(current_id) if current_id else None,
                "releases": rows,
                "total_count": len(rows),
            }
        )

    def post(self, request, challenge_id):
        # 공급망 publish bundle 한 벌을 릴리스로 등록한다
        try:
            validated = validate_release_payload(request.data)
        except ReleaseValidationError as error:
            return fail("RELEASE_INVALID", error.message, 400)

        with transaction.atomic():
            challenge = _get_challenge(challenge_id, for_update=True)
            if challenge is None:
                return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

            try:
                check_slug_consistency(challenge, validated["challenge_slug"])
            except ReleaseValidationError as error:
                return fail("RELEASE_INVALID", error.message, 400)

            duplicated = ChallengeRelease.objects.filter(
                challenge=challenge,
                registry_revision=validated["registry_revision"],
            ).exists()
            if duplicated:
                return fail(
                    "RELEASE_DUPLICATED",
                    "같은 registry_revision의 릴리스가 이미 등록되어 있습니다.",
                    409,
                )

            release = create_release(challenge, validated, request.user.login_id)

        return ok(
            serialize_release(release, _current_release_id(challenge)),
            message="릴리스가 등록되었습니다.",
        )


class ReleaseActivateView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, challenge_id, release_id):
        # 지정한 릴리스를 현재 배포 버전으로 전환한다. 옛 버전 지정이 곧 롤백이다
        with transaction.atomic():
            challenge = _get_challenge(challenge_id, for_update=True)
            if challenge is None:
                return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

            release = (
                ChallengeRelease.objects
                .prefetch_related("containers")
                .filter(challenge=challenge, release_id=release_id)
                .first()
            )
            if release is None:
                return fail("RELEASE_NOT_FOUND", "존재하지 않는 릴리스 ID입니다.", 404)

            if not is_deployable(release):
                return fail(
                    "RELEASE_NOT_DEPLOYABLE",
                    "현재 Scheduler 계약으로 배포할 수 없는 릴리스입니다.",
                    400,
                )

            config, _ = ChallengeRuntimeConfig.objects.select_for_update().get_or_create(
                challenge=challenge
            )

            previous_release_id = config.current_release_id
            if previous_release_id == release.release_id:
                previous_release_id = None
            else:
                config.current_release = release
                config.save(update_fields=["current_release", "updated_at"])

        return ok(
            {
                "challenge_id": str(challenge.challenge_id),
                "release_id": str(release.release_id),
                "version": release.version,
                "registry_revision": release.registry_revision,
                "previous_release_id": (
                    str(previous_release_id) if previous_release_id else None
                ),
                "activated_at": isoformat_z(timezone.now().replace(microsecond=0)),
            },
            message="현재 릴리스가 전환되었습니다.",
        )
