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


def scheduler_auth_header(request=None):
    # Scheduler 호출에 사용할 내부 API 토큰 헤더를 만든다
    if not settings.SCHEDULER_API_TOKEN:
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "인스턴스 서버 설정이 올바르지 않습니다.",
            503,
        )

    return f"Bearer {settings.SCHEDULER_API_TOKEN}"


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
        # settings에서 URL scheme과 authority를 검증한 배포 설정만 사용한다.
        with urlopen(  # nosec B310
            request, timeout=settings.SCHEDULER_TIMEOUT_SECONDS
        ) as response:
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


def release_registry_revision(release):
    # PR #26 registry_revision과 기존 revision 필드를 모두 지원한다
    revision = getattr(release, "registry_revision", None)
    if revision is None:
        revision = getattr(release, "revision", None)

    if not isinstance(revision, int) or revision <= 0:
        raise SchedulerError("INVALID_REQUEST", "릴리즈 revision 설정을 확인해주세요.", 400)

    return revision


def release_container_image(container):
    # Registry 내부 image_ref를 Scheduler의 image 필드로 변환한다
    return getattr(container, "image_ref", getattr(container, "image", None))


def release_container_ports(container):
    # PR #26 포트 객체 배열과 기존 정수 배열을 Scheduler 포트 배열로 변환한다
    ports = []
    for entry in container.ports:
        if isinstance(entry, dict):
            ports.append(entry.get("port"))
        else:
            ports.append(entry)

    return ports


def release_container_expose(container):
    # PR #26 public 포트 정보를 Scheduler의 expose 값으로 변환한다
    if hasattr(container, "expose"):
        return container.expose

    return any(entry.get("public") for entry in container.ports if isinstance(entry, dict))


def serialize_release_container(container):
    # Scheduler create 요청에 들어갈 컨테이너 정보를 생성한다
    return {
        "name": container.name,
        "image": release_container_image(container),
        "ports": release_container_ports(container),
        "expose": release_container_expose(container),
    }


def build_scheduler_create_body(user, team, challenge, runtime_config):
    # Scheduler 인스턴스 생성 요청 body를 만든다
    release = runtime_config.current_release
    validate_release_for_scheduler(release)
    containers = release.containers.order_by("name")

    return {
        "team_id": str(team.team_id),
        "user_id": str(user.user_id),
        "challenge_id": str(challenge.challenge_id),
        "containers": [serialize_release_container(container) for container in containers],
        "registry_revision": release_registry_revision(release),
        "isolation_profile": getattr(release, "isolation_profile", "WEB"),
        "architecture": release.architecture,
        "resource_profile": {
            "cpu_millicores": release.cpu_millicores,
            "memory_mib": release.memory_mib,
            "ephemeral_storage_mib": release.ephemeral_storage_mib,
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


def create_instance_from_scheduler(scheduler_data, user, team, challenge=None, release=None, replaced_instance=None):
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
            "release": release,
        },
    )
    return instance


def sync_instance_from_scheduler(instance, auth_header=None):
    # 최종 상태가 아닌 인스턴스를 Scheduler 최신 상태로 동기화한다
    if instance is None or instance.status in FINAL_INSTANCE_STATUSES:
        return instance

    scheduler_data = call_scheduler_detail(instance, auth_header)
    return update_instance_from_scheduler(instance, scheduler_data)


def validate_release_for_scheduler(release):
    # Scheduler create 요청 전에 릴리즈 컨테이너 조건을 확인한다
    if release is None:
        raise SchedulerError("ACTIVE_RELEASE_NOT_FOUND", "활성화된 문제 릴리즈가 없습니다.", 404)

    containers = list(release.containers.all())
    exposed = [container for container in containers if release_container_expose(container)]

    if not 1 <= len(containers) <= 8:
        raise SchedulerError("INVALID_REQUEST", "컨테이너 설정을 확인해주세요.", 400)

    if len(exposed) != 1:
        raise SchedulerError("INVALID_REQUEST", "공개 컨테이너 설정을 확인해주세요.", 400)

    if len(release_container_ports(exposed[0])) != 1:
        raise SchedulerError("INVALID_REQUEST", "공개 컨테이너 포트 설정을 확인해주세요.", 400)

    for container in containers:
        image = release_container_image(container)
        ports = release_container_ports(container)
        if not str(image).startswith("ghcr.io/") or "@sha256:" not in str(image):
            raise SchedulerError("INVALID_REQUEST", "컨테이너 이미지 설정을 확인해주세요.", 400)
        if not ports or any(not isinstance(port, int) for port in ports):
            raise SchedulerError("INVALID_REQUEST", "컨테이너 포트 설정을 확인해주세요.", 400)
