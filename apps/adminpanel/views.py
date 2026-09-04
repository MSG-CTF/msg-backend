import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q, Sum
from django.utils import timezone

from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import Team, User
from apps.common.exceptions import InvalidRequest, TeamBanned
from apps.common.permissions import IsAdmin
from apps.common.response import fail, ok
from apps.common.utils import num
from apps.common.jwt import hash_token
from apps.common.idempotency import run_idempotent
from apps.challenge.models import Challenge, Solve
from apps.board.models import TeamBoardState
from apps.timer.models import Contest

from apps.teams.models import (
    MileageHistory,
    MileageType,
    PaymentToken,
    PaymentTokenStatus,
)

from .exceptions import (
    AlreadyBanned,
    AlreadyRefunded,
    InsufficientMileage,
    InvalidAmount,
    NotBanned,
    NotRefundable,
    PaymentNotFound,
    PaymentTokenExpired,
    PaymentTokenInvalid,
    TeamNotFound,
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


@api_view(["GET"])
@permission_classes([IsAdmin])
def challenge_list(request):
    """GET /api/v1/admin/challenges. 문제 목록 + 문제별 인스턴스 현황."""
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
