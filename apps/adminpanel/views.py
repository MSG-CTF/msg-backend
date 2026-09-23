import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q, Sum
from django.utils import timezone
from datetime import datetime

from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import Role, Team, User
from apps.common.exceptions import InvalidRequest, TeamBanned
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.common.utils import num
from apps.common.jwt import hash_token
from apps.common.idempotency import run_idempotent
from apps.challenge.models import Challenge, Solve
from apps.challenge.services import hash_flag
from apps.board.models import (
    Cell,
    PendingDiceRoll,
    TeamBoardState,
    TeamCellCandidate,
    TeamCellConsumption,
    TeamChallengeAccess,
    TeamChanceCard,
)
from apps.board.services import (
    FIRST_CELL_INDEX,
    LAST_CELL_INDEX,
    MAX_DICE_ROLLS,
    apply_pending_dice_recharge,
    get_or_create_board_state,
)
from apps.timer.models import Contest
from .models import AdminEvent, AdminSetting

from apps.teams.models import (
    MileageHistory,
    MileageType,
    PaymentToken,
    PaymentTokenStatus,
)

from .exceptions import (
    AlreadyBanned,
    AlreadyRefunded,
    InsufficientDice,
    InsufficientMileage,
    InvalidAmount,
    LoginIdTaken,
    NotBanned,
    NotRefundable,
    PaymentNotFound,
    PaymentTokenExpired,
    PaymentTokenInvalid,
    TeamNotFound,
    TeamAlreadyHasLeader,
    TeamNameTaken,
    ContestAlreadyStarted,
)

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
from .serializers import ChallengeCreateSerializer

SORT_FIELDS = {
    "score": "-team_score",
    "name": "team_name",
}

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_PAGE = 10_000
MAX_BAN_REASON_LENGTH = 500
MILEAGE_TYPES = set(MileageType.values)

DICE_ADJUST_MIN = -20
DICE_ADJUST_MAX = 20

SETTING_SPECS = {
    "board.dice_rolls_per_reset": (1, 20, 3),
    "board.dice_reset_interval_minutes": (1, 1440, 15),
    "board.solve_deadline_minutes": (1, 180, 15),
    "flag.max_attempts": (1, 10, 3),
    "flag.lock_seconds": (1, 3600, 30),
}
SETTINGS_UPDATED_KEY = "_meta.updated"
EVENT_TYPES = set(AdminEvent.EventType.values)

CELL_STATUS_UNVISITED = "UNVISITED"
CELL_STATUS_CONSUMED = "CONSUMED"
CELL_STATUS_OPENED = "OPENED"
CELL_STATUS_CLEARED = "CLEARED"
CELL_STATUSES = (
    CELL_STATUS_UNVISITED,
    CELL_STATUS_CONSUMED,
    CELL_STATUS_OPENED,
    CELL_STATUS_CLEARED,
)


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

def _get_team(team_id):
    try:
        return Team.objects.get(pk=team_id)
    except (Team.DoesNotExist, ValidationError, ValueError):
        raise TeamNotFound()


def _get_team_for_update(team_id):
    try:
        return Team.objects.select_for_update().get(pk=team_id)
    except (Team.DoesNotExist, ValidationError, ValueError):
        raise TeamNotFound()

@api_view(["POST"])
@permission_classes([IsAdmin])
def account_create(request):
    login_id = request.data.get("login_id")
    if not isinstance(login_id, str) or not login_id.strip():
        raise InvalidRequest("필수 항목이 누락되었습니다: login_id")
    login_id = login_id.strip()
    if len(login_id) > 50:
        raise InvalidRequest("login_id 는 50자 이하여야 합니다")

    password = request.data.get("password")
    if not isinstance(password, str) or not (8 <= len(password) <= 128):
        raise InvalidRequest("password 는 8자 이상 128자 이하여야 합니다")

    nickname = request.data.get("nickname")
    if not isinstance(nickname, str) or not nickname.strip():
        raise InvalidRequest("필수 항목이 누락되었습니다: nickname")
    nickname = nickname.strip()
    if len(nickname) > 50:
        raise InvalidRequest("nickname 은 50자 이하여야 합니다")

    role = request.data.get("role", Role.PARTICIPANT)
    if role not in Role.values:
        raise InvalidRequest("role 이 올바르지 않습니다")

    is_leader = request.data.get("is_leader", False)
    if not isinstance(is_leader, bool):
        raise InvalidRequest("is_leader 는 boolean 이어야 합니다")

    if role == Role.ADMIN and is_leader:
        raise InvalidRequest("관리자 계정은 팀장이 될 수 없습니다")

    team_id = request.data.get("team_id")
    team_name = request.data.get("team_name")
    if "team_id" in request.data and "team_name" in request.data:
        raise InvalidRequest("team_id 와 team_name 은 함께 보낼 수 없습니다")

    team = None
    if team_id:
        try:
            team = Team.objects.get(pk=team_id)
        except (Team.DoesNotExist, ValidationError, ValueError):
            raise TeamNotFound()
    elif team_name is not None:
        if not isinstance(team_name, str) or not team_name.strip():
            raise InvalidRequest("team_name 은 1자 이상이어야 합니다")
        team_name = team_name.strip()
        if len(team_name) > 100:
            raise InvalidRequest("team_name 은 100자 이하여야 합니다")
        if Team.objects.filter(team_name=team_name).exists():
            raise TeamNameTaken()

    if User.objects.filter(login_id=login_id).exists():
        raise LoginIdTaken()

    if is_leader and team is not None and User.objects.filter(team=team, is_leader=True).exists():
        raise TeamAlreadyHasLeader()

    try:
        with transaction.atomic():
            if team is None and team_name:
                team = Team.objects.create(team_name=team_name)
            user = User.objects.create_user(
                login_id=login_id,
                password=password,
                nickname=nickname,
                role=role,
                team=team,
                is_leader=is_leader,
            )
    except IntegrityError as exc:
        text = str(exc)
        if "uq_users_one_leader_per_team" in text:
            raise TeamAlreadyHasLeader()
        if "team_name" in text:
            raise TeamNameTaken()
        raise LoginIdTaken()

    return ok(
        {
            "user_id": str(user.user_id),
            "login_id": user.login_id,
            "nickname": user.nickname,
            "role": user.role,
            "is_leader": user.is_leader,
            "team_id": str(user.team_id) if user.team_id else None,
            "team_name": team.team_name if team else None,
            "created_at": user.created_at,
        },
        message="계정이 등록되었습니다",
    )

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

@api_view(["PATCH"])
@permission_classes([IsAdmin])
def challenge_visibility(request, challenge_id):
    is_published = request.data.get("is_published")
    if not isinstance(is_published, bool):
        raise InvalidRequest("is_published 는 boolean 이어야 합니다")

    reason = request.data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidRequest("필수 항목이 누락되었습니다: reason")
    reason = reason.strip()
    if len(reason) > 500:
        raise InvalidRequest("reason 은 500자 이하여야 합니다")

    try:
        with transaction.atomic():
            challenge = Challenge.objects.select_for_update().get(pk=challenge_id)
            previous = challenge.is_published
            challenge.is_published = is_published
            challenge.save(update_fields=["is_published"])
            affected_team_count = TeamChallengeAccess.objects.filter(challenge=challenge).count()
    except (Challenge.DoesNotExist, ValidationError, ValueError):
        return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

    return ok(
        {
            "challenge_id": str(challenge.challenge_id),
            "title": challenge.title,
            "previous_is_published": previous,
            "is_published": challenge.is_published,
            "affected_team_count": affected_team_count,
            "changed_at": timezone.now().replace(microsecond=0),
            "changed_by": request.user.login_id,
        },
        message="문제 공개 상태가 변경되었습니다",
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

    def work():
        team = _get_team_for_update(team_id)
        previous = team.mileage

        if amount < 0 and previous + amount < 0:
            raise InsufficientMileage(
                data={
                    "current_mileage": previous,
                    "requested_amount": -amount,
                }
            )

        mtype = MileageType.ADMIN_GRANT if amount > 0 else MileageType.ADMIN_DEDUCT
        now = timezone.now().replace(microsecond=0)

        MileageHistory.objects.create(
            team=team,
            type=mtype,
            amount=amount,
            reason=reason,
            processed_by=request.user.login_id,
        )
        team.mileage = previous + amount
        team.save(update_fields=["mileage", "updated_at"])

        return {
            "team_id": str(team.team_id),
            "previous_mileage": previous,
            "amount": amount,
            "current_mileage": team.mileage,
            "reason": reason,
            "adjusted_at": now,
            "adjusted_by": request.user.login_id,
        }

    return run_idempotent(
        request,
        {"amount": amount, "reason": reason},
        work,
        message="마일리지가 조정되었습니다",
    )

@api_view(["GET"])
@permission_classes([IsAdmin])
def mileage_history(request):
    queryset = MileageHistory.objects.select_related("team")

    team_id = request.query_params.get("team_id")
    if team_id:
        try:
            uuid.UUID(str(team_id))
        except (ValueError, TypeError, AttributeError):
            raise InvalidRequest("team_id 형식이 올바르지 않습니다")
        queryset = queryset.filter(team_id=team_id)

    mtype = request.query_params.get("type")
    if mtype:
        if mtype not in MILEAGE_TYPES:
            raise InvalidRequest("type 이 올바르지 않습니다")
        queryset = queryset.filter(type=mtype)

    page = _page_number(request.query_params.get("page"), 1, MAX_PAGE)
    size = min(_page_number(request.query_params.get("size"), DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)

    total_count = queryset.count()
    offset = (page - 1) * size
    rows = queryset.order_by("-created_at", "-history_id")[offset : offset + size]

    history = [
        {
            "history_id": str(r.history_id),
            "team_id": str(r.team_id),
            "team_name": r.team.team_name,
            "type": r.type,
            "amount": r.amount,
            "reason": r.reason,
            "processed_by": r.processed_by,
            "created_at": r.created_at,
        }
        for r in rows
    ]

    return ok({"history": history, "total_count": total_count, "page": page, "size": size})


@api_view(["POST"])
@permission_classes([IsAdmin])
def payment_checkout(request):
    raw_token = request.data.get("payment_token")
    if not raw_token or not isinstance(raw_token, str):
        raise InvalidRequest("필수 항목이 누락되었습니다: payment_token")

    amount = request.data.get("amount")
    if amount is None:
        raise InvalidRequest("필수 항목이 누락되었습니다: amount")
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise InvalidRequest("amount 는 정수여야 합니다")
    if amount <= 0:
        raise InvalidAmount("결제 금액은 1 이상이어야 합니다")

    item_name = request.data.get("item_name")
    if not item_name or not isinstance(item_name, str) or not item_name.strip():
        raise InvalidRequest("필수 항목이 누락되었습니다: item_name")
    item_name = item_name.strip()

    now = timezone.now().replace(microsecond=0)

    with transaction.atomic():
        token = (
            PaymentToken.objects.select_for_update()
            .filter(token_hash=hash_token(raw_token))
            .first()
        )
        if token is None or token.status != PaymentTokenStatus.ACTIVE:
            raise PaymentTokenInvalid()
        if token.expires_at < now:
            raise PaymentTokenExpired()

        team = Team.objects.select_for_update().get(pk=token.team_id)
        if team.is_banned:
            raise TeamBanned()
        if team.mileage < amount:
            raise InsufficientMileage(
                data={"current_mileage": team.mileage, "requested_amount": amount}
            )

        history = MileageHistory.objects.create(
            team=team,
            type=MileageType.PURCHASE,
            amount=-amount,
            reason=item_name,
            item_name=item_name,
            processed_by=request.user.login_id,
        )
        team.mileage -= amount
        team.save(update_fields=["mileage", "updated_at"])

        token.status = PaymentTokenStatus.USED
        token.used_at = now
        token.history = history
        token.save(update_fields=["status", "used_at", "history"])

    return ok(
        {
            "history_id": str(history.history_id),
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "item_name": item_name,
            "amount": -amount,
            "current_mileage": team.mileage,
            "processed_at": now,
            "processed_by": request.user.login_id,
        },
        message="결제가 완료되었습니다",
    )

@api_view(["GET"])
@permission_classes([IsAdmin])
def payment_history(request):
    page = _page_number(request.query_params.get("page"), 1, MAX_PAGE)
    size = min(_page_number(request.query_params.get("size"), 50), MAX_PAGE_SIZE)

    queryset = MileageHistory.objects.filter(
        type__in=[MileageType.PURCHASE, MileageType.REFUND]
    ).select_related("team")

    team_id = request.query_params.get("team_id")
    if team_id:
        try:
            uuid.UUID(str(team_id))
        except (ValueError, TypeError):
            raise InvalidRequest("team_id 형식이 올바르지 않습니다")
        queryset = queryset.filter(team_id=team_id)

    total_count = queryset.count()

    offset = (page - 1) * size
    rows = list(queryset.order_by("-created_at")[offset : offset + size])


    history = [
        {
            "history_id": str(r.history_id),
            "team_id": str(r.team_id),
            "team_name": r.team.team_name,
            "type": r.type,
            "amount": r.amount,
            "reason": r.reason,
            "is_refunded": r.is_refunded,
            "processed_by": r.processed_by,
            "created_at": r.created_at,
        }
        for r in rows
    ]

    return ok(
        {"history": history, "total_count": total_count, "page": page, "size": size}
    )


@api_view(["DELETE"])
@permission_classes([IsAdmin])
def payment_refund(request, history_id):
    now = timezone.now().replace(microsecond=0)

    with transaction.atomic():
        try:
            purchase = MileageHistory.objects.select_for_update().get(pk=history_id)
        except (MileageHistory.DoesNotExist, ValidationError, ValueError):
            raise PaymentNotFound()

        if purchase.type != MileageType.PURCHASE:
            raise NotRefundable()

        existing = (
            MileageHistory.objects.filter(type=MileageType.REFUND, ref_history=purchase)
            .order_by("created_at")
            .first()
        )
        if existing is not None:
            raise AlreadyRefunded(
                data={
                    "history_id": str(purchase.history_id),
                    "refunded_at": existing.created_at,
                }
            )

        refunded_amount = -purchase.amount  # PURCHASE.amount 는 음수 → 양수 환불액
        team = Team.objects.select_for_update().get(pk=purchase.team_id)

        refund = MileageHistory.objects.create(
            team=team,
            type=MileageType.REFUND,
            amount=refunded_amount,
            reason=f"결제 환불 (history_id: {purchase.history_id})",
            ref_history=purchase,
            processed_by=request.user.login_id,
        )
        team.mileage += refunded_amount
        team.save(update_fields=["mileage", "updated_at"])

        purchase.is_refunded = True
        purchase.save(update_fields=["is_refunded"])

    return ok(
        {
            "history_id": str(refund.history_id),
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "refunded_amount": refunded_amount,
            "current_mileage": team.mileage,
            "refunded_at": now,
            "refunded_by": request.user.login_id,
        },
        message="환불이 완료되었습니다",
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

    # 대량 인스턴스에서 페이지네이션 시 3중 집계를 매번 돌지 않도록 opt-out 허용.
    # 기본은 포함(true), ?summary=false 면 집계를 건너뛰고 summary=null.
    include_summary = request.query_params.get("summary", "true").lower() != "false"

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
            "summary": _instance_summary() if include_summary else None,
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


@api_view(["GET"])
@permission_classes([IsAdmin])
def team_detail(request, team_id):
    """GET /api/v1/admin/teams/{team_id}. 팀 상세 조회."""
    raw_limit = request.query_params.get("history_limit")
    if raw_limit in (None, ""):
        history_limit = 10
    else:
        try:
            history_limit = int(raw_limit)
        except (TypeError, ValueError):
            raise InvalidRequest("history_limit 은 정수여야 합니다")
        history_limit = max(1, min(history_limit, 50))

    try:
        team = Team.objects.select_related("board_state").prefetch_related(
            Prefetch("members", queryset=User.objects.order_by("-is_leader", "nickname"))
        ).get(pk=team_id)
    except (Team.DoesNotExist, ValidationError, ValueError):
        raise TeamNotFound()

    members = list(team.members.all())

    try:
        board_position = team.board_state.position_id
    except TeamBoardState.DoesNotExist:
        board_position = None

    agg = MileageHistory.objects.filter(team=team).aggregate(
        earned=Sum("amount", filter=Q(amount__gt=0)),
        spent=Sum("amount", filter=Q(amount__lt=0)),
        purchase=Count("history_id", filter=Q(type=MileageType.PURCHASE)),
        refund=Count("history_id", filter=Q(type=MileageType.REFUND)),
    )
    recent = MileageHistory.objects.filter(team=team).order_by(
        "-created_at", "-history_id"
    )[:history_limit]

    return ok(
        {
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "team_score": num(team.team_score),
            "mileage": team.mileage,
            "board_position_states": board_position,
            "is_banned": team.is_banned,
            "ban_reason": team.ban_reason,
            "banned_at": team.banned_at,
            "banned_by": team.banned_by,
            "created_at": team.created_at,
            "member_count": len(members),
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
            "mileage_summary": {
                "total_earned": agg["earned"] or 0,
                "total_spent": abs(agg["spent"] or 0),
                "purchase_count": agg["purchase"],
                "refund_count": agg["refund"],
            },
            "recent_mileage_history": [
                {
                    "history_id": str(r.history_id),
                    "type": r.type,
                    "amount": r.amount,
                    "reason": r.reason,
                    "processed_by": r.processed_by,
                    "created_at": r.created_at,
                }
                for r in recent
            ],
        }
    )

@api_view(["GET"])
@permission_classes([IsAdmin])
def dashboard(request):
    now = timezone.now().replace(microsecond=0)

    team_agg = Team.objects.aggregate(
        total=Count("team_id"),
        banned=Count("team_id", filter=Q(is_banned=True)),
        mileage=Sum("mileage"),
    )
    pay = MileageHistory.objects.aggregate(
        purchase=Count("history_id", filter=Q(type=MileageType.PURCHASE)),
        refund=Count("history_id", filter=Q(type=MileageType.REFUND)),
        pay_sum=Sum(
            "amount",
            filter=Q(type__in=[MileageType.PURCHASE, MileageType.REFUND]),
        ),
    )
    inst = Instance.objects.aggregate(
        running=Count("instance_id", filter=Q(status=InstanceStatus.RUNNING)),
        failed=Count("instance_id", filter=Q(status=InstanceStatus.FAILED)),
        total=Count("instance_id"),
    )

    contest = Contest.objects.filter(is_active=True).first()
    if contest is None:
        contest_data = None
    else:
        snap = contest.snapshot(now)
        contest_data = {
            "status": snap["status"],
            "start_time": contest.start_time,
            "end_time": contest.end_time,
            "remaining_seconds": snap["remaining_seconds"],
        }

    return ok(
        {
            "teams": {
                "total_count": team_agg["total"],
                "banned_count": team_agg["banned"],
                "total_mileage": team_agg["mileage"] or 0,
            },
            "payment": {
                "purchase_count": pay["purchase"],
                "refund_count": pay["refund"],
                "net_spent": -(pay["pay_sum"] or 0),
            },
            "contest": contest_data,
            "instances": {
                "running": inst["running"],
                "failed": inst["failed"],
                "total": inst["total"],
            },
            "challenges": {
                "total": Challenge.objects.count(),
                "published": Challenge.objects.filter(is_published=True).count(),
                "solved_total": Solve.objects.count(),
            },
            "collected_at": now,
        }
    )

CHALLENGE_SORT = {
    "running": "-running_instance_count",
    "title": "title",
    "score": "-score",
}


def _challenge_create(request):
    serializer = ChallengeCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        with transaction.atomic():
            challenge = Challenge.objects.create(
                challenge_slug=data["challenge_slug"],
                title=data["title"],
                category=data["category"],
                difficulty=data["difficulty"],
                description=data.get("description"),
                flag_hash=hash_flag(data["flag"]),
                score=data["initial_score"],
                initial_score=data["initial_score"],
                minimum_score=data["minimum_score"],
                decay=data["decay"],
                current_score=data["initial_score"],
                is_published=False,
            )
    except IntegrityError as error:
        raise InvalidRequest("이미 사용 중인 challenge_slug입니다.") from error

    return ok(
        {
            "challenge_id": str(challenge.challenge_id),
            "challenge_slug": challenge.challenge_slug,
            "title": challenge.title,
            "category": challenge.category,
            "difficulty": challenge.difficulty,
            "description": challenge.description,
            "score": num(challenge.score),
            "initial_score": num(challenge.initial_score),
            "minimum_score": num(challenge.minimum_score),
            "decay": challenge.decay,
            "current_score": num(challenge.current_score),
            "is_published": challenge.is_published,
            "created_at": challenge.created_at,
        },
        message="문제가 등록되었습니다.",
    )


@api_view(["GET", "POST"])
@permission_classes([IsAdmin])
def challenge_list(request):
    """관리자 문제 등록 또는 문제별 인스턴스 현황 조회."""
    if request.method == "POST":
        return _challenge_create(request)

    sort = request.query_params.get("sort", "running")
    if sort not in CHALLENGE_SORT:
        raise InvalidRequest("정렬 기준이 올바르지 않습니다. (running, title, score 중 선택)")

    category = request.query_params.get("category")
    if category and category not in Challenge.CategoryType.values:
        raise InvalidRequest("카테고리가 올바르지 않습니다")

    page = _page_number(request.query_params.get("page"), 1, MAX_PAGE)
    size = min(_page_number(request.query_params.get("size"), 50), MAX_PAGE_SIZE)

    queryset = Challenge.objects.annotate(
        solved_team_count=Count("solves", distinct=True),
        running_instance_count=Count(
            "instances",
            filter=Q(instances__status=InstanceStatus.RUNNING),
            distinct=True,
        ),
        failed_instance_count=Count(
            "instances",
            filter=Q(instances__status=InstanceStatus.FAILED),
            distinct=True,
        ),
    )
    if category:
        queryset = queryset.filter(category=category)

    is_published = request.query_params.get("is_published")
    if is_published is not None:
        if is_published.lower() not in ("true", "false"):
            raise InvalidRequest("is_published 는 true 또는 false 여야 합니다")
        queryset = queryset.filter(is_published=(is_published.lower() == "true"))

    total_count = queryset.count()
    offset = (page - 1) * size
    rows = queryset.order_by(CHALLENGE_SORT[sort], "title")[offset : offset + size]

    challenges = [
        {
            "challenge_id": str(c.challenge_id),
            "challenge_slug": c.challenge_slug,
            "title": c.title,
            "category": c.category,
            "difficulty": c.difficulty,
            "score": num(c.score),
            "is_published": c.is_published,
            "solved_team_count": c.solved_team_count,
            "running_instance_count": c.running_instance_count,
            "failed_instance_count": c.failed_instance_count,
        }
        for c in rows
    ]

    return ok(
        {"challenges": challenges, "total_count": total_count, "page": page, "size": size}
    )

@api_view(["POST"])
@permission_classes([IsAdmin])
def board_dice(request, team_id):
    amount = request.data.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise InvalidRequest("amount 는 정수여야 합니다")
    if amount == 0:
        raise InvalidAmount("조정할 횟수는 0이 될 수 없습니다")
    if not (DICE_ADJUST_MIN <= amount <= DICE_ADJUST_MAX):
        raise InvalidRequest(f"amount 는 {DICE_ADJUST_MIN} ~ {DICE_ADJUST_MAX} 범위여야 합니다")

    reason = request.data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidRequest("필수 항목이 누락되었습니다: reason")
    reason = reason.strip()
    if len(reason) > 500:
        raise InvalidRequest("reason 은 500자 이하여야 합니다")

    with transaction.atomic():
        # 보드와 같은 순서로 잠근다: 보드 상태 먼저, 팀은 잠그지 않는다.
        team = _get_team(team_id)
        get_or_create_board_state(team)
        state = TeamBoardState.objects.select_for_update(of=("self",)).get(team=team)
        apply_pending_dice_recharge(state)

        previous = state.dice_rolls_left
        if amount < 0 and previous + amount < 0:
            raise InsufficientDice(
                data={"current_dice_rolls_left": previous, "requested_amount": -amount}
            )
        # 보드 보상(grant_dice_roll)과 같은 규칙으로 보유 상한을 넘기지 않는다.
        applied = max(0, min(amount, MAX_DICE_ROLLS - previous)) if amount > 0 else amount
        state.dice_rolls_left = previous + applied
        state.save(update_fields=["dice_rolls_left", "updated_at"])
        # 조정 뒤 충전 시계를 보드와 같은 규칙으로 다시 맞춘다.
        apply_pending_dice_recharge(state)

    return ok(
        {
            "team_id": str(team.team_id),
            "previous_dice_rolls_left": previous,
            "amount": applied,
            "dice_rolls_left": state.dice_rolls_left,
            "reason": reason,
            "adjusted_at": timezone.now().replace(microsecond=0),
            "adjusted_by": request.user.login_id,
        },
        message="주사위 횟수가 조정되었습니다",
    )


def _parse_iso_utc(raw, field):
    if not isinstance(raw, str):
        raise InvalidRequest(f"{field} 는 ISO-8601 문자열이어야 합니다")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidRequest(f"{field} 형식이 올바르지 않습니다")
    if timezone.is_naive(parsed):
        raise InvalidRequest(f"{field} 는 UTC 오프셋을 포함해야 합니다")
    return parsed


def _settings_payload():
    stored = {row.key: row for row in AdminSetting.objects.all()}
    grouped = {}
    for key, (_, _, default) in SETTING_SPECS.items():
        group, name = key.split(".", 1)
        grouped.setdefault(group, {})[name] = stored[key].value if key in stored else default

    contest = Contest.objects.filter(is_active=True).first()
    if contest is None:
        contest_data = {"status": "BEFORE", "started_at": None, "ends_at": None}
    else:
        contest_data = {
            "status": contest.snapshot()["status"],
            "started_at": contest.start_time,
            "ends_at": contest.end_time,
        }

    marker = stored.get(SETTINGS_UPDATED_KEY)
    return {
        "contest": contest_data,
        "board": grouped.get("board", {}),
        "flag": grouped.get("flag", {}),
        "updated_at": marker.updated_at if marker else None,
        "updated_by": marker.updated_by if marker else None,
    }


@api_view(["GET", "PATCH"])
@permission_classes([IsAdmin])
def settings_view(request):
    if request.method == "GET":
        return ok(_settings_payload())

    body = request.data
    if not isinstance(body, dict) or not body:
        raise InvalidRequest("변경할 설정이 없습니다")

    unknown_groups = set(body) - {"board", "flag", "contest"}
    if unknown_groups:
        raise InvalidRequest(f"알 수 없는 항목입니다: {', '.join(sorted(unknown_groups))}")

    changes = {}
    for group in ("board", "flag"):
        if group not in body:
            continue
        values = body[group]
        if not isinstance(values, dict):
            raise InvalidRequest(f"{group} 은 객체여야 합니다")
        for name, value in values.items():
            key = f"{group}.{name}"
            if key not in SETTING_SPECS:
                raise InvalidRequest(f"알 수 없는 설정입니다: {key}")
            if not isinstance(value, int) or isinstance(value, bool):
                raise InvalidRequest(f"{key} 는 정수여야 합니다")
            low, high, _ = SETTING_SPECS[key]
            if not (low <= value <= high):
                raise InvalidRequest(f"{key} 는 {low} ~ {high} 범위여야 합니다")
            changes[key] = value

    contest_body = None
    if "contest" in body:
        contest_body = body["contest"]
        if not isinstance(contest_body, dict):
            raise InvalidRequest("contest 는 객체여야 합니다")

    with transaction.atomic():
        if contest_body:
            unknown = set(contest_body) - {"started_at", "ends_at"}
            if unknown:
                raise InvalidRequest(f"알 수 없는 설정입니다: {', '.join(sorted(unknown))}")

            contest = Contest.objects.select_for_update().filter(is_active=True).first()
            if contest is None:
                raise InvalidRequest("활성화된 대회가 없습니다")

            now = timezone.now()
            started = contest.start_time <= now
            if "started_at" in contest_body:
                new_start = _parse_iso_utc(contest_body["started_at"], "started_at")
                if started and new_start != contest.start_time:
                    raise ContestAlreadyStarted()
                contest.start_time = new_start
            if "ends_at" in contest_body:
                contest.end_time = _parse_iso_utc(contest_body["ends_at"], "ends_at")

            if contest.end_time <= contest.start_time:
                raise InvalidRequest("ends_at 은 started_at 보다 뒤여야 합니다")
            contest.save(update_fields=["start_time", "end_time"])

        # 항목 순서가 다른 동시 요청이 서로의 잠금을 기다리지 않도록 항상 같은 순서로 저장한다.
        for key in sorted(changes):
            AdminSetting.objects.update_or_create(
                key=key,
                defaults={"value": changes[key], "updated_by": request.user.login_id},
            )

        AdminSetting.objects.update_or_create(
            key=SETTINGS_UPDATED_KEY,
            defaults={"value": 0, "updated_by": request.user.login_id},
        )

    return ok(_settings_payload(), message="설정이 변경되었습니다")


@api_view(["GET"])
@permission_classes([IsAdmin])
def event_list(request):
    queryset = AdminEvent.objects.select_related("team", "challenge")

    etype = request.query_params.get("type")
    if etype:
        if etype not in EVENT_TYPES:
            return fail("INVALID_REQUEST", "이벤트 타입이 올바르지 않습니다.", 400)
        queryset = queryset.filter(type=etype)

    team_id = request.query_params.get("team_id")
    if team_id:
        try:
            uuid.UUID(str(team_id))
        except (ValueError, TypeError, AttributeError):
            raise InvalidRequest("team_id 형식이 올바르지 않습니다")
        queryset = queryset.filter(team_id=team_id)

    page = _page_number(request.query_params.get("page"), 1, MAX_PAGE)
    size = min(_page_number(request.query_params.get("size"), 50), MAX_PAGE_SIZE)

    total_count = queryset.count()
    offset = (page - 1) * size
    rows = queryset[offset : offset + size]

    events = [
        {
            "event_id": str(e.event_id),
            "type": e.type,
            "severity": e.severity,
            "message": e.message,
            "team_id": str(e.team_id) if e.team_id else None,
            "team_name": e.team.team_name if e.team_id else None,
            "challenge_id": str(e.challenge_id) if e.challenge_id else None,
            "challenge_title": e.challenge.title if e.challenge_id else None,
            "instance_id": str(e.instance_id) if e.instance_id else None,
            "actor": e.actor,
            "created_at": e.created_at,
        }
        for e in rows
    ]

    return ok({"events": events, "total_count": total_count, "page": page, "size": size})


def _json_object(request):
    """본문이 객체가 아니면 .get 에서 터져 500 이 나간다. 400 으로 돌려준다."""
    body = request.data
    if not isinstance(body, dict):
        raise InvalidRequest("요청 본문은 JSON 객체여야 합니다")
    return body


def _require_reason(body):
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidRequest("필수 항목이 누락되었습니다: reason")
    reason = reason.strip()
    if len(reason) > 500:
        raise InvalidRequest("reason 은 500자 이하여야 합니다")
    return reason


def _parse_cell_index(raw):
    """URL 에서 받은 칸 번호. 음수나 문자도 404 가 아니라 400 으로 돌려준다."""
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        raise InvalidRequest("cell_index 는 정수여야 합니다")
    cell_index = int(raw)
    if not (FIRST_CELL_INDEX <= cell_index <= LAST_CELL_INDEX):
        raise InvalidRequest(
            f"cell_index 는 {FIRST_CELL_INDEX} ~ {LAST_CELL_INDEX} 범위여야 합니다"
        )
    return cell_index


def _get_cell(cell_index):
    cell = Cell.objects.filter(cell_index=cell_index).first()
    if cell is None:
        raise InvalidRequest(f"{cell_index} 번 칸이 없습니다")
    return cell


def _read_cell_status(team, cell):
    """보드의 build_cell_states 와 같은 규칙. 단 소모 기록 없이 문제만 열린 칸도
    UNVISITED 가 아니라 실제 상태로 읽는다. 지우는 대상을 감춰서는 안 된다."""
    access = TeamChallengeAccess.objects.filter(team=team, source_cell=cell).first()
    if access is not None:
        if access.status == TeamChallengeAccess.Status.CLEARED:
            return CELL_STATUS_CLEARED
        return CELL_STATUS_OPENED
    if TeamCellConsumption.objects.filter(team=team, cell=cell).exists():
        return CELL_STATUS_CONSUMED
    return CELL_STATUS_UNVISITED


def _drop_cell_challenge(state, team, cell, access):
    """칸을 되돌린다. 개방 기록이 남으면 그 칸에서 문제를 다시 열 수 없으므로 후보까지 지운다."""
    TeamCellCandidate.objects.filter(team=team, cell=cell).delete()
    if access is None:
        return
    if state.active_challenge_access_id == access.id:
        state.active_challenge_access = None
        state.save(update_fields=["active_challenge_access", "updated_at"])
    access.delete()


@api_view(["PATCH"])
@permission_classes([IsAdmin])
def board_position(request, team_id):
    body = _json_object(request)

    position = body.get("position")
    if not isinstance(position, int) or isinstance(position, bool):
        raise InvalidRequest("position 은 정수여야 합니다")
    if not (FIRST_CELL_INDEX <= position <= LAST_CELL_INDEX):
        raise InvalidRequest(
            f"position 은 {FIRST_CELL_INDEX} ~ {LAST_CELL_INDEX} 범위여야 합니다"
        )

    consume_cell = body.get("consume_cell", False)
    if not isinstance(consume_cell, bool):
        raise InvalidRequest("consume_cell 은 true 또는 false 여야 합니다")

    _require_reason(body)

    with transaction.atomic():
        # 보드와 같은 순서로 잠근다: 보드 상태 먼저, 팀은 잠그지 않는다.
        team = _get_team(team_id)
        get_or_create_board_state(team)
        state = TeamBoardState.objects.select_for_update(of=("self",)).get(team=team)

        cell = _get_cell(position)
        previous_position = state.position_id
        state.position = cell
        state.save(update_fields=["position", "updated_at"])

        # 확정되지 않은 굴림이 남아 있으면 팀이 확정하는 순간 교정한 위치가 덮어써진다.
        # 선택을 기다리던 찬스카드를 함께 마감하지 않으면 이후 모든 확정이 막힌다.
        TeamChanceCard.objects.filter(
            team=team,
            used_at__isnull=True,
            discarded_at__isnull=True,
            pending_first_number__isnull=False,
        ).update(
            used_at=timezone.now(),
            pending_first_number=None,
            pending_second_number=None,
        )
        PendingDiceRoll.objects.filter(team=team).delete()

        if consume_cell:
            TeamCellConsumption.objects.get_or_create(team=team, cell=cell)
        apply_pending_dice_recharge(state)
        # 도착 칸의 효과는 발동하지 않는다. 소모 여부만 현재 값으로 돌려준다.
        cell_consumed = TeamCellConsumption.objects.filter(team=team, cell=cell).exists()

    return ok(
        {
            "team_id": str(team.team_id),
            "previous_position": previous_position,
            "position": cell.cell_index,
            "type": cell.type,
            "cell_consumed": cell_consumed,
            "moved_at": timezone.now().replace(microsecond=0),
            "moved_by": request.user.login_id,
        },
        message="말 위치가 변경되었습니다",
    )


@api_view(["PATCH"])
@permission_classes([IsAdmin])
def board_cell_status(request, team_id, cell_index):
    cell_index = _parse_cell_index(cell_index)
    body = _json_object(request)

    status = body.get("status")
    if status not in CELL_STATUSES:
        raise InvalidRequest(f"status 는 {', '.join(CELL_STATUSES)} 중 하나여야 합니다")

    _require_reason(body)

    with transaction.atomic():
        # 보드와 같은 순서로 잠근다: 보드 상태 먼저, 그 다음 문제 개방 기록.
        team = _get_team(team_id)
        get_or_create_board_state(team)
        state = TeamBoardState.objects.select_for_update(of=("self",)).get(team=team)

        cell = _get_cell(cell_index)
        previous_status = _read_cell_status(team, cell)
        access = (
            TeamChallengeAccess.objects.select_for_update()
            .filter(team=team, source_cell=cell)
            .first()
        )

        if status == CELL_STATUS_UNVISITED:
            _drop_cell_challenge(state, team, cell, access)
            TeamCellConsumption.objects.filter(team=team, cell=cell).delete()
        elif status == CELL_STATUS_CONSUMED:
            _drop_cell_challenge(state, team, cell, access)
            TeamCellConsumption.objects.get_or_create(team=team, cell=cell)
        else:
            if access is None:
                raise InvalidRequest(
                    "이 칸에서 연 문제가 없어 OPENED 나 CLEARED 로 바꿀 수 없습니다"
                )
            TeamCellConsumption.objects.get_or_create(team=team, cell=cell)
            if status == CELL_STATUS_CLEARED:
                access.status = TeamChallengeAccess.Status.CLEARED
                access.cleared_at = timezone.now()
                if state.active_challenge_access_id == access.id:
                    state.active_challenge_access = None
                    state.save(update_fields=["active_challenge_access", "updated_at"])
            else:
                access.status = TeamChallengeAccess.Status.OPENED
                access.cleared_at = None
            access.save(update_fields=["status", "cleared_at"])

        apply_pending_dice_recharge(state)

    return ok(
        {
            "team_id": str(team.team_id),
            "cell_index": cell.cell_index,
            "previous_status": previous_status,
            "status": status,
            "changed_at": timezone.now().replace(microsecond=0),
            "changed_by": request.user.login_id,
        },
        message="칸 상태가 변경되었습니다",
    )
