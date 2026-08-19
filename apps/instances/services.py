import json
from datetime import timezone as dt_timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils.dateparse import parse_datetime

from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRuntimeConfig, Instance, InstanceStatus


ACTIVE_INSTANCE_STATUSES = [
    InstanceStatus.REQUESTED,
    InstanceStatus.SCHEDULING,
    InstanceStatus.PROVISIONING,
    InstanceStatus.RUNNING,
    InstanceStatus.RESTARTING,
    InstanceStatus.RESETTING,
]

RESETTABLE_INSTANCE_STATUSES = [
    InstanceStatus.RUNNING,
]

DELETABLE_INSTANCE_STATUSES = [
    InstanceStatus.RUNNING,
]

EXTENDABLE_INSTANCE_STATUSES = [
    InstanceStatus.RUNNING,
]

FINAL_INSTANCE_STATUSES = [
    InstanceStatus.STOPPED,
    InstanceStatus.FAILED,
    InstanceStatus.EXPIRED,
    InstanceStatus.CLEANED,
]


class SchedulerError(Exception):
    def __init__(self, code, message, status_code):
        # Scheduler 호출 실패를 API 응답 코드로 전달하기 위해 사용한다
        self.code = code
        self.message = message
        self.status_code = status_code


def isoformat_z(value):
    # datetime 값을 API 응답용 UTC 문자열로 바꾼다
    if value is None:
        return None

    return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def parse_scheduler_datetime(value):
    # Scheduler가 내려준 ISO datetime 문자열을 Django datetime으로 바꾼다
    if not value:
        return None

    return parse_datetime(value)


def scheduler_auth_header(request):
    # 사용자의 Authorization 헤더를 Scheduler 호출에 그대로 전달한다
    if request is None:
        return None

    return request.headers.get("Authorization")


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
        "expires_at": isoformat_z(instance.expires_at),
        "hard_expires_at": isoformat_z(instance.hard_expires_at),
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


def scheduler_request(method, path, body=None, auth_header=None, query=None):
    # Scheduler HTTP API를 호출하고 공통 응답 data를 반환한다
    base_url = settings.SCHEDULER_BASE_URL.rstrip("/")
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    payload = None
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header

    if body is not None:
        payload = json.dumps(body).encode("utf-8")

    request = Request(url, data=payload, headers=headers, method=method)

    try:
        with urlopen(request, timeout=settings.SCHEDULER_TIMEOUT_SECONDS) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as error:
        raise scheduler_error_from_response(error) from error
    except (TimeoutError, URLError) as error:
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "인스턴스 서버와 연결할 수 없습니다. 잠시 후 다시 시도해주세요.",
            503,
        ) from error

    if not response_body:
        return None

    result = json.loads(response_body)
    return result.get("data")


def scheduler_error_from_response(error):
    # Scheduler 에러 응답을 백엔드 공통 에러 형식으로 변환한다
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}

    if error.code >= 500:
        return SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "인스턴스 서버와 연결할 수 없습니다. 잠시 후 다시 시도해주세요.",
            503,
        )

    return SchedulerError(
        payload.get("code", "INTERNAL_ERROR"),
        payload.get("message", "요청 처리 중 오류가 발생했습니다."),
        error.code,
    )


def build_scheduler_create_body(user, team, challenge, runtime_config):
    # Scheduler 인스턴스 생성 요청 body를 만든다
    return {
        "team_id": str(team.team_id),
        "user_id": str(user.user_id),
        "challenge_id": str(challenge.challenge_id),
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


def call_scheduler_create(user, team, challenge, runtime_config, auth_header=None):
    # Scheduler에 인스턴스 생성을 요청한다
    return scheduler_request(
        "POST",
        "/api/instances",
        body=build_scheduler_create_body(user, team, challenge, runtime_config),
        auth_header=auth_header,
    )


def call_scheduler_delete(instance, auth_header=None):
    # Scheduler에 인스턴스 삭제를 요청한다
    return scheduler_request(
        "DELETE",
        f"/api/instances/{instance.instance_id}",
        body={},
        auth_header=auth_header,
    )


def call_scheduler_reset(instance, auth_header=None):
    # Scheduler에 인스턴스 초기화를 요청한다
    return scheduler_request(
        "POST",
        f"/api/instances/{instance.instance_id}/reset",
        body={},
        auth_header=auth_header,
    )


def call_scheduler_extend(instance, auth_header=None):
    # Scheduler에 인스턴스 시간 연장을 요청한다
    return scheduler_request(
        "POST",
        f"/api/instances/{instance.instance_id}/extend",
        body={"extend_minutes": settings.INSTANCE_EXTEND_MINUTES},
        auth_header=auth_header,
    )


def call_scheduler_detail(instance, auth_header=None):
    # Scheduler에서 인스턴스 단건 상태를 조회한다
    return scheduler_request(
        "GET",
        f"/api/instances/{instance.instance_id}",
        auth_header=auth_header,
    )


def call_scheduler_active(user, auth_header=None):
    # Scheduler에서 현재 사용자의 active instance를 조회한다
    return scheduler_request(
        "GET",
        "/api/instances/active",
        auth_header=auth_header,
        query={"user_id": str(user.user_id)},
    )


def update_instance_from_scheduler(instance, scheduler_data):
    # Scheduler 응답값으로 백엔드 인스턴스 상태를 갱신한다
    if not scheduler_data:
        return instance

    update_fields = ["updated_at"]

    instance.status = scheduler_data.get("status", instance.status)
    update_fields.append("status")

    if "service_url" in scheduler_data:
        instance.host = scheduler_data.get("service_url")
        instance.ports = []
        update_fields.extend(["host", "ports"])

    if "expires_at" in scheduler_data:
        instance.expires_at = parse_scheduler_datetime(scheduler_data.get("expires_at"))
        update_fields.append("expires_at")

    if "hard_expires_at" in scheduler_data:
        instance.hard_expires_at = parse_scheduler_datetime(scheduler_data.get("hard_expires_at"))
        update_fields.append("hard_expires_at")

    instance.save(update_fields=update_fields)
    return instance


def create_instance_from_scheduler(scheduler_data, user, team, challenge=None, replaced_instance=None):
    # Scheduler가 발급한 instance_id로 백엔드 인스턴스 row를 만든다
    if challenge is None:
        challenge = Challenge.objects.filter(challenge_id=scheduler_data.get("challenge_id")).first()

    instance, _ = Instance.objects.update_or_create(
        instance_id=scheduler_data["instance_id"],
        defaults={
            "user": user,
            "team": team,
            "challenge": challenge,
            "status": scheduler_data.get("status", InstanceStatus.REQUESTED),
            "host": scheduler_data.get("service_url"),
            "ports": [],
            "expires_at": parse_scheduler_datetime(scheduler_data.get("expires_at")),
            "hard_expires_at": parse_scheduler_datetime(scheduler_data.get("hard_expires_at")),
            "replaced_instance": replaced_instance,
        },
    )
    return instance


def sync_instance_from_scheduler(instance, auth_header=None):
    # 최종 상태가 아닌 인스턴스를 Scheduler 최신 상태로 동기화한다
    if instance is None or instance.status in FINAL_INSTANCE_STATUSES:
        return instance

    scheduler_data = call_scheduler_detail(instance, auth_header)
    return update_instance_from_scheduler(instance, scheduler_data)
