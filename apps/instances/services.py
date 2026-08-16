import json
from datetime import timezone as dt_timezone

from django_redis import get_redis_connection

from apps.instances.models import ChallengeRuntimeConfig, DeleteReason, Instance, InstanceStatus


INSTANCE_JOB_QUEUE = "instance_jobs"

ACTIVE_INSTANCE_STATUSES = [
    InstanceStatus.REQUESTED,
    InstanceStatus.SCHEDULING,
    InstanceStatus.PROVISIONING,
    InstanceStatus.RUNNING,
    InstanceStatus.RESTARTING,
    InstanceStatus.RESETTING,
    InstanceStatus.STOPPING,
]

RESETTABLE_INSTANCE_STATUSES = [
    InstanceStatus.RUNNING,
    InstanceStatus.FAILED,
]

DELETABLE_INSTANCE_STATUSES = [
    InstanceStatus.REQUESTED,
    InstanceStatus.SCHEDULING,
    InstanceStatus.PROVISIONING,
    InstanceStatus.RUNNING,
    InstanceStatus.RESTARTING,
    InstanceStatus.RESETTING,
]

EXTENDABLE_INSTANCE_STATUSES = [
    InstanceStatus.RUNNING,
]


def isoformat_z(value):
    # datetime 값을 API 응답용 UTC 문자열로 바꾼다
    if value is None:
        return None

    return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def get_active_instance(user):
    # 현재 사용자의 최신 활성 인스턴스 한 개를 조회한다
    return (
        Instance.objects
        .filter(user=user, status__in=ACTIVE_INSTANCE_STATUSES)
        .select_related("challenge")
        .order_by("-created_at")
        .first()
    )


def get_challenge_runtime_config(challenge):
    # 문제의 컨테이너 실행 설정을 조회한다
    return ChallengeRuntimeConfig.objects.filter(challenge=challenge).first()


def serialize_instance(instance, include_title=False, include_replaced=False):
    # 인스턴스 응답 데이터를 API 명세 형식으로 정리한다
    if instance is None:
        return None

    is_running = instance.status == InstanceStatus.RUNNING
    data = {
        "instance_id": str(instance.instance_id),
        "challenge_id": str(instance.challenge_id),
        "host": instance.host if is_running else None,
        "ports": instance.ports if is_running else [],
        "status": instance.status,
        "expires_at": isoformat_z(instance.expires_at) if is_running else None,
    }

    if include_title:
        data["challenge_title"] = instance.challenge.title

    if include_replaced:
        data["replaced_instance_id"] = (
            str(instance.replaced_instance_id)
            if instance.replaced_instance_id
            else None
        )

    return data


def build_create_payload(instance, runtime_config):
    # Scheduler가 CREATE 작업을 처리할 수 있는 Redis payload를 만든다
    return {
        "action": "CREATE",
        "instance_id": str(instance.instance_id),
        "user_id": str(instance.user_id),
        "team_id": str(instance.team_id),
        "challenge_id": str(instance.challenge_id),
        "container_image": runtime_config.container_image,
        "container_port": runtime_config.container_port,
        "architecture": runtime_config.architecture,
        "resource_profile": {
            "cpu_millicores": runtime_config.cpu_millicores,
            "memory_mib": runtime_config.memory_mib,
            "ephemeral_storage_mib": runtime_config.ephemeral_storage_mib,
        },
        "ttl_minutes": runtime_config.ttl_minutes,
        "hard_timeout_minutes": runtime_config.hard_timeout_minutes,
    }


def build_delete_payload(instance, delete_reason=DeleteReason.USER_REQUESTED):
    # Scheduler가 DELETE 작업을 처리할 수 있는 Redis payload를 만든다
    return {
        "action": "DELETE",
        "instance_id": str(instance.instance_id),
        "user_id": str(instance.user_id),
        "team_id": str(instance.team_id),
        "challenge_id": str(instance.challenge_id),
        "delete_reason": delete_reason,
    }


def build_reset_payload(instance):
    # Scheduler가 RESET 작업을 처리할 수 있는 Redis payload를 만든다
    return {
        "action": "RESET",
        "instance_id": str(instance.instance_id),
        "user_id": str(instance.user_id),
        "team_id": str(instance.team_id),
        "challenge_id": str(instance.challenge_id),
    }


def build_extend_payload(instance):
    # Scheduler가 EXTEND 작업을 처리할 수 있는 Redis payload를 만든다
    return {
        "action": "EXTEND",
        "instance_id": str(instance.instance_id),
        "user_id": str(instance.user_id),
        "team_id": str(instance.team_id),
        "challenge_id": str(instance.challenge_id),
    }


def enqueue_instance_job(payload):
    # 인스턴스 작업 payload를 Redis queue에 적재한다
    redis = get_redis_connection("default")
    redis.lpush(INSTANCE_JOB_QUEUE, json.dumps(payload))
