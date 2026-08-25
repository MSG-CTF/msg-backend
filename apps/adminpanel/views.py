import uuid

from django.db.models import Count, Prefetch
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from rest_framework.decorators import api_view, permission_classes

from apps.common.response import fail, ok
from apps.accounts.models import Team, User
from apps.common.exceptions import InvalidRequest
from apps.common.permissions import IsAdmin
from apps.common.response import ok
from apps.common.utils import num

from apps.teams.models import MileageHistory, MileageType

from .exceptions import AlreadyBanned, InsufficientMileage, InvalidAmount, NotBanned, TeamNotFound

from apps.instances.models import (
    DeleteReason,
    Instance,
    InstanceLock,
    InstanceStatus,
)

from apps.instances.services import (
    DELETABLE_INSTANCE_STATUSES,
    RESETTABLE_INSTANCE_STATUSES,
    SchedulerError,
    call_scheduler_delete,
    call_scheduler_reset,
    create_instance_from_scheduler,
    isoformat_z,
    scheduler_auth_header,
)

SORT_FIELDS = {
    "score": "-team_score",
    "name": "team_name",
}

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_PAGE = 10_000
MAX_BAN_REASON_LENGTH = 500


def _page_number(raw, default, maximum=None):
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidRequest("page 와 size 는 정수여야 합니다")
    if value < 1:
        raise InvalidRequest("page 와 size 는 1 이상이어야 합니다")
    if maximum and value > maximum:
        raise InvalidRequest(f"page 는 {maximum} 이하여야 합니다")
    return value


@api_view(["GET"])
@permission_classes([IsAdmin])
def team_list(request):
    search = request.query_params.get("search", "").strip()

    sort = request.query_params.get("sort", "score")
    if sort not in SORT_FIELDS:
        raise InvalidRequest("정렬 기준이 올바르지 않습니다. (score, name 중 선택)")

    page = _page_number(request.query_params.get("page"), 1, MAX_PAGE)
    size = min(_page_number(request.query_params.get("size"), DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)

    queryset = Team.objects.prefetch_related(
        Prefetch("members", queryset=User.objects.order_by("-is_leader", "nickname"))
    )
    if search:
        queryset = queryset.filter(team_name__icontains=search)

    total_count = queryset.count()

    offset = (page - 1) * size
    rows = queryset.order_by(SORT_FIELDS[sort], "team_name")[offset : offset + size]

    teams = []
    for team in rows:
        members = list(team.members.all())
        teams.append(
            {
                "team_id": str(team.team_id),
                "team_name": team.team_name,
                "team_score": num(team.team_score),
                "mileage": team.mileage,
                # 보드 앱이 생기면 team_board_states.position 으로 채운다.
                "board_position_states": None,
                "is_banned": team.is_banned,
                "members": [
                    {
                        "user_id": str(m.user_id),
                        "login_id": m.login_id,
                        "nickname": m.nickname,
                        "role": m.role,
                        "is_leader": m.is_leader,
                    }
                    for m in members
                ],
                "member_count": len(members),
            }
        )
        

    return ok({"teams": teams, "total_count": total_count, "page": page, "size": size})

def _get_team_for_update(team_id):
    try:
        return Team.objects.select_for_update().get(pk=team_id)
    except (Team.DoesNotExist, ValidationError, ValueError):
        raise TeamNotFound()


@api_view(["POST", "DELETE"])
@permission_classes([IsAdmin])
def team_ban(request, team_id):
    if request.method == "POST":
        return _ban(request, team_id)
    return _unban(request, team_id)


def _ban(request, team_id):
    reason = request.data.get("ban_reason")
    if reason is None:
        raise InvalidRequest("필수 항목이 누락되었습니다: ban_reason")
    if not isinstance(reason, str):
        raise InvalidRequest("ban_reason 은 문자열이어야 합니다")
    reason = reason.strip()
    if not reason:
        raise InvalidRequest("벤 사유는 1자 이상 입력해야 합니다")
    if len(reason) > MAX_BAN_REASON_LENGTH:
        raise InvalidRequest(f"벤 사유는 {MAX_BAN_REASON_LENGTH}자 이하여야 합니다")

    with transaction.atomic():
        team = _get_team_for_update(team_id)
        if team.is_banned:
            raise AlreadyBanned(
                data={
                    "team_id": str(team.team_id),
                    "ban_reason": team.ban_reason,
                    "banned_at": team.banned_at,
                }
            )
        team.is_banned = True
        team.ban_reason = reason
        team.banned_at = timezone.now().replace(microsecond=0)
        team.banned_by = request.user.login_id
        team.save(
            update_fields=["is_banned", "ban_reason", "banned_at", "banned_by", "updated_at"]
        )

    return ok(
        {
            "team_id": str(team.team_id),
            "is_banned": True,
            "ban_reason": team.ban_reason,
            "banned_at": team.banned_at,
            "banned_by": team.banned_by,
        },
        message="팀 활동이 정지되었습니다",
    )


def _unban(request, team_id):
    with transaction.atomic():
        team = _get_team_for_update(team_id)
        if not team.is_banned:
            raise NotBanned(data={"team_id": str(team.team_id), "is_banned": False})

        # 이력이 필요하면 admin_events 에 기록한다 (해당 앱 생성 후).
        team.is_banned = False
        team.ban_reason = None
        team.banned_at = None
        team.banned_by = None
        team.save(
            update_fields=["is_banned", "ban_reason", "banned_at", "banned_by", "updated_at"]
        )

    return ok(
        {
            "team_id": str(team.team_id),
            "is_banned": False,
            "unbanned_at": timezone.now().replace(microsecond=0),
            "unbanned_by": request.user.login_id,
        },
        message="팀 활동 정지가 해제되었습니다",
    )
@api_view(["POST"])
@permission_classes([IsAdmin])
def team_mileage(request, team_id):
    amount = request.data.get("amount")
    if amount is None:
        raise InvalidRequest("필수 항목이 누락되었습니다: amount")
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise InvalidRequest("amount 는 정수여야 합니다")
    if amount == 0:
        raise InvalidAmount()

    reason = request.data.get("reason")
    if reason is None:
        raise InvalidRequest("필수 항목이 누락되었습니다: reason")
    if not isinstance(reason, str):
        raise InvalidRequest("reason 은 문자열이어야 합니다")
    reason = reason.strip()
    if not reason:
        raise InvalidRequest("reason 은 1자 이상 입력해야 합니다")
    if len(reason) > 500:
        raise InvalidRequest("reason 은 500자 이하여야 합니다")

    with transaction.atomic():
        team = _get_team_for_update(team_id)
        previous = team.mileage

        if amount < 0 and previous + amount < 0:
            raise InsufficientMileage(
                data={
                    "current_mileage": previous,
                    "requested_amount": -amount
                }
            )

        mtype = MileageType.ADMIN_GRANT if amount > 0 else MileageType.ADMIN_DEDUCT
        now = timezone.now().replace(microsecond=0)

        # 불변식: 아래 두 줄이 한 트랜잭션 안에서 함께 일어나야 한다.
        # mileage_history 총합 == team.mileage
        MileageHistory.objects.create(
            team=team,
            type=mtype,
            amount=amount,
            reason=reason,
            processed_by=request.user.login_id,
        )
        team.mileage = previous + amount
        team.save(update_fields=["mileage", "updated_at"])

    return ok(
        {
            "team_id": str(team.team_id),
            "previous_mileage": previous,
            "amount": amount,
            "current_mileage": team.mileage,
            "reason": reason,
            "adjusted_at": now,
            "adjusted_by": request.user.login_id,
        },
        message="마일리지가 조정되었습니다",
    )

INSTANCE_STATUS_VALUES = set(InstanceStatus.values)


def _lock_instance_owner(user):
    InstanceLock.objects.select_for_update().get_or_create(user=user)


def _instance_summary():
    by_status = {s: 0 for s in InstanceStatus.values}
    for row in Instance.objects.values("status").annotate(c=Count("instance_id")):
        by_status[row["status"]] = row["c"]

    running = Instance.objects.filter(status=InstanceStatus.RUNNING)
    by_team = [
        {
            "team_id": str(r["team_id"]),
            "team_name": r["team__team_name"],
            "running_count": r["c"],
        }
        for r in running.values("team_id", "team__team_name")
        .annotate(c=Count("instance_id"))
        .order_by("-c", "team__team_name")
    ]
    by_challenge = [
        {
            "challenge_id": str(r["challenge_id"]),
            "challenge_title": r["challenge__title"],
            "running_count": r["c"],
        }
        for r in running.values("challenge_id", "challenge__title")
        .annotate(c=Count("instance_id"))
        .order_by("-c", "challenge__title")
    ]
    return {"by_status": by_status, "by_team": by_team, "by_challenge": by_challenge}


@api_view(["GET"])
@permission_classes([IsAdmin])
def instance_list(request):
    status_filter = request.query_params.get("status")
    if status_filter and status_filter not in INSTANCE_STATUS_VALUES:
        return fail("INVALID_REQUEST", "상태 값이 올바르지 않습니다", 400)

    team_id = request.query_params.get("team_id")
    challenge_id = request.query_params.get("challenge_id")
    for raw in (team_id, challenge_id):
        if raw:
            try:
                uuid.UUID(str(raw))
            except (ValueError, TypeError):
                return fail("INVALID_REQUEST", "요청 값이 올바르지 않습니다", 400)

    page = _page_number(request.query_params.get("page"), 1, MAX_PAGE)
    size = min(_page_number(request.query_params.get("size"), 50), MAX_PAGE_SIZE)

    queryset = Instance.objects.select_related("team", "challenge")
    if status_filter:
        queryset = queryset.filter(status=status_filter)
    if team_id:
        queryset = queryset.filter(team_id=team_id)
    if challenge_id:
        queryset = queryset.filter(challenge_id=challenge_id)

    total_count = queryset.count()
    offset = (page - 1) * size
    rows = queryset.order_by("-created_at")[offset : offset + size]

    instances = [
        {
            "instance_id": str(r.instance_id),
            "team_id": str(r.team_id),
            "team_name": r.team.team_name,
            "challenge_id": str(r.challenge_id),
            "challenge_title": r.challenge.title,
            "status": r.status,
            "created_at": isoformat_z(r.created_at),
            "expires_at": isoformat_z(r.expires_at),
        }
        for r in rows
    ]

    return ok(
        {
            "instances": instances,
            "summary": _instance_summary(),
            "total_count": total_count,
            "page": page,
            "size": size,
        }
    )


@api_view(["DELETE"])
@permission_classes([IsAdmin])
def instance_force_delete(request, instance_id):
    now = timezone.now().replace(microsecond=0)

    owner = Instance.objects.select_related("user").filter(instance_id=instance_id).first()
    if owner is None:
        return fail("INSTANCE_NOT_FOUND", "존재하지 않는 인스턴스 ID입니다", 404)

    with transaction.atomic():
        _lock_instance_owner(owner.user)

        instance = (
            Instance.objects.select_for_update()
            .select_related("team")
            .filter(instance_id=instance_id)
            .first()
        )
        if instance is None:
            return fail("INSTANCE_NOT_FOUND", "존재하지 않는 인스턴스 ID입니다", 404)

        if instance.status not in DELETABLE_INSTANCE_STATUSES:
            return fail(
                "INSTANCE_ALREADY_TERMINATED",
                "이미 종료된 인스턴스입니다",
                409,
                data={"instance_id": str(instance.instance_id), "status": instance.status},
            )

        try:
            call_scheduler_delete(instance, scheduler_auth_header(request))
        except SchedulerError as error:
            return fail(error.code, error.message, error.status_code)

        instance.status = InstanceStatus.STOPPING
        instance.delete_reason = DeleteReason.ADMIN_FORCED
        instance.save(update_fields=["status", "delete_reason", "updated_at"])

    return ok(
        {
            "instance_id": str(instance.instance_id),
            "team_id": str(instance.team_id),
            "team_name": instance.team.team_name,
            "status": instance.status,
            "forced_by": request.user.login_id,
            "forced_at": isoformat_z(now),
        },
        message="인스턴스 종료 요청이 접수되었습니다.",
        status=202,
    )

@api_view(["POST"])
@permission_classes([IsAdmin])
def instance_force_reset(request, instance_id):
    now = timezone.now().replace(microsecond=0)

    owner = (
        Instance.objects.select_related("user")
        .filter(instance_id=instance_id)
        .first()
    )
    if owner is None:
        return fail("INSTANCE_NOT_FOUND", "존재하지 않는 인스턴스 ID입니다", 404)

    with transaction.atomic():
        _lock_instance_owner(owner.user)

        instance = (
            Instance.objects.select_for_update()
            .select_related("team", "challenge")
            .filter(instance_id=instance_id)
            .first()
        )
        if instance is None:
            return fail("INSTANCE_NOT_FOUND", "존재하지 않는 인스턴스 ID입니다", 404)

        if instance.status not in RESETTABLE_INSTANCE_STATUSES:
            return fail(
                "INSTANCE_NOT_RESTARTABLE",
                "재시작할 수 없는 상태입니다.",
                409,
                data={"instance_id": str(instance.instance_id), "status": instance.status},
            )

        try:
            scheduler_data = call_scheduler_reset(instance, scheduler_auth_header(request))
        except SchedulerError as error:
            return fail(error.code, error.message, error.status_code)

        new_instance = create_instance_from_scheduler(
            scheduler_data,
            user=instance.user,
            team=instance.team,
            challenge=instance.challenge,
            replaced_instance=instance,
        )

    return ok(
        {
            "instance_id": str(new_instance.instance_id),
            "team_id": str(new_instance.team_id),
            "team_name": instance.team.team_name,
            "challenge_id": str(new_instance.challenge_id),
            "status": new_instance.status,
            "host": new_instance.host if new_instance.status == InstanceStatus.RUNNING else None,
            "port": None,
            "expires_at": isoformat_z(new_instance.expires_at),
            "forced_by": request.user.login_id,
            "forced_at": isoformat_z(now),
        },
        message="인스턴스 재시작 요청이 접수되었습니다.",
        status=202,
    )