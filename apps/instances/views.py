from django.db import transaction
from rest_framework.views import APIView

from apps.challenge.models import Challenge
from apps.common.response import fail, ok
from apps.instances.models import DeleteReason, Instance, InstanceStatus
from apps.instances.services import (
    ACTIVE_INSTANCE_STATUSES,
    DELETABLE_INSTANCE_STATUSES,
    EXTENDABLE_INSTANCE_STATUSES,
    RESETTABLE_INSTANCE_STATUSES,
    build_create_payload,
    build_delete_payload,
    build_extend_payload,
    build_reset_payload,
    enqueue_instance_job,
    get_active_instance,
    get_challenge_runtime_config,
    serialize_instance,
)

MAX_EXTEND_COUNT = 3


class InstanceCreateView(APIView):
    def post(self, request):
        # 새 인스턴스 생성 요청을 저장하고 Scheduler용 CREATE 작업을 Redis queue에 적재한다
        user = request.user
        team = getattr(user, "team", None)
        if team is None:
            return fail("USER_HAS_NO_TEAM", "소속된 팀이 없습니다", 404)

        challenge_id = request.data.get("challenge_id")
        if not challenge_id:
            return fail("INVALID_REQUEST", "요청 값이 올바르지 않습니다", 400)

        challenge = Challenge.objects.filter(challenge_id=challenge_id).first()
        if challenge is None:
            return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

        runtime_config = get_challenge_runtime_config(challenge)
        if runtime_config is None:
            return fail("RUNTIME_CONFIG_NOT_FOUND", "문제 실행 설정이 없습니다.", 404)

        with transaction.atomic():
            active_instance = (
                Instance.objects
                .select_for_update()
                .filter(user=user, status__in=ACTIVE_INSTANCE_STATUSES)
                .order_by("-created_at")
                .first()
            )
            replaced_instance = None

            if active_instance is not None:
                replaced_instance = active_instance
                replaced_instance.status = InstanceStatus.STOPPING
                replaced_instance.delete_reason = DeleteReason.REPLACED_BY_NEW_INSTANCE
                replaced_instance.save(update_fields=["status", "delete_reason", "updated_at"])
                enqueue_instance_job(
                    build_delete_payload(
                        replaced_instance,
                        DeleteReason.REPLACED_BY_NEW_INSTANCE,
                    )
                )

            instance = Instance.objects.create(
                user=user,
                team=team,
                challenge=challenge,
                status=InstanceStatus.REQUESTED,
                replaced_instance=replaced_instance,
            )
            enqueue_instance_job(build_create_payload(instance, runtime_config))

        message = "인스턴스 생성 요청이 접수되었습니다."
        if replaced_instance is not None:
            message = "인스턴스 생성 요청이 접수되었습니다. 기존 인스턴스가 종료됩니다."

        return ok(
            serialize_instance(instance, include_replaced=True),
            message=message,
            status=202,
        )


class InstanceDeleteView(APIView):
    def delete(self, request, instance_id):
        # 본인 소유 인스턴스 종료 요청을 Redis queue에 적재한다
        instance = Instance.objects.filter(instance_id=instance_id).first()
        if instance is None:
            return fail("INSTANCE_NOT_FOUND", "존재하지 않는 인스턴스 ID입니다.", 404)

        if instance.user_id != request.user.user_id:
            return fail("FORBIDDEN", "권한이 필요합니다", 403)

        if instance.status not in DELETABLE_INSTANCE_STATUSES:
            return fail("INVALID_STATE_TRANSITION", "현재 상태에서는 요청을 처리할 수 없습니다.", 400)

        instance.status = InstanceStatus.STOPPING
        instance.delete_reason = DeleteReason.USER_REQUESTED
        instance.save(update_fields=["status", "delete_reason", "updated_at"])
        enqueue_instance_job(build_delete_payload(instance, DeleteReason.USER_REQUESTED))

        return ok(
            {
                "instance_id": str(instance.instance_id),
                "challenge_id": str(instance.challenge_id),
                "status": instance.status,
            },
            message="인스턴스 종료 요청이 접수되었습니다.",
            status=202,
        )


class InstanceResetView(APIView):
    def post(self, request, instance_id):
        # 본인 소유 인스턴스 초기화 요청을 Redis queue에 적재한다
        instance = Instance.objects.filter(instance_id=instance_id).select_related("challenge").first()
        if instance is None:
            return fail("INSTANCE_NOT_FOUND", "존재하지 않는 인스턴스 ID입니다.", 404)

        if instance.user_id != request.user.user_id:
            return fail("FORBIDDEN", "권한이 필요합니다", 403)

        if instance.status not in RESETTABLE_INSTANCE_STATUSES:
            return fail("INVALID_STATE_TRANSITION", "현재 상태에서는 요청을 처리할 수 없습니다.", 400)

        instance.status = InstanceStatus.RESETTING
        instance.host = None
        instance.ports = []
        instance.expires_at = None
        instance.hard_expires_at = None
        instance.save(
            update_fields=[
                "status",
                "host",
                "ports",
                "expires_at",
                "hard_expires_at",
                "updated_at",
            ]
        )
        enqueue_instance_job(build_reset_payload(instance))

        return ok(
            serialize_instance(instance),
            message="인스턴스 초기화 요청이 접수되었습니다.",
            status=202,
        )


class InstanceExtendView(APIView):
    def post(self, request, instance_id):
        # 본인 소유 인스턴스 TTL 연장 요청을 Redis queue에 적재한다
        instance = Instance.objects.filter(instance_id=instance_id).select_related("challenge").first()
        if instance is None:
            return fail("INSTANCE_NOT_FOUND", "존재하지 않는 인스턴스 ID입니다.", 404)

        if instance.user_id != request.user.user_id:
            return fail("FORBIDDEN", "권한이 필요합니다", 403)

        if instance.status not in EXTENDABLE_INSTANCE_STATUSES:
            return fail("INVALID_STATE_TRANSITION", "현재 상태에서는 요청을 처리할 수 없습니다.", 400)

        if instance.extend_count >= MAX_EXTEND_COUNT:
            return fail("TTL_EXTENSION_LIMIT_EXCEEDED", "더 이상 인스턴스 시간을 연장할 수 없습니다.", 400)

        instance.extend_count += 1
        instance.save(update_fields=["extend_count", "updated_at"])
        enqueue_instance_job(build_extend_payload(instance))

        return ok(
            {
                "instance_id": str(instance.instance_id),
                "challenge_id": str(instance.challenge_id),
                "status": instance.status,
                "expires_at": serialize_instance(instance)["expires_at"],
            },
            message="TTL 연장 요청이 접수되었습니다.",
            status=202,
        )


class MyInstanceView(APIView):
    def get(self, request):
        # 현재 access token의 user_id 기준 활성 인스턴스 한 개를 조회한다
        instance = get_active_instance(request.user)
        if instance is None:
            return ok(None, message="현재 실행 중인 인스턴스가 없습니다.")

        return ok(
            serialize_instance(instance, include_title=True),
            message="인스턴스 상태 조회 성공",
        )
