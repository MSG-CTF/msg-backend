import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.db.models import Sum
from django.utils import timezone

from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import Team, User
from apps.common.exceptions import InvalidRequest
from apps.common.permissions import IsAdmin
from apps.common.response import ok
from apps.common.utils import num
from apps.common.jwt import hash_token

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


@api_view(["POST"])
@permission_classes([IsAdmin])
def payment_checkout(request):
    """POST /api/v1/admin/payment/checkout — QR 스캔 결제 처리."""
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
            queryset = queryset.none()
        else:
            queryset = queryset.filter(team_id=team_id)

    total_count = queryset.count()

    offset = (page - 1) * size
    rows = list(queryset.order_by("-created_at")[offset : offset + size])

    purchase_ids = [r.history_id for r in rows if r.type == MileageType.PURCHASE]
    refunded_ids = set(
        MileageHistory.objects.filter(
            type=MileageType.REFUND, ref_history_id__in=purchase_ids
        ).values_list("ref_history_id", flat=True)
    )

    history = [
        {
            "history_id": str(r.history_id),
            "team_id": str(r.team_id),
            "team_name": r.team.team_name,
            "type": r.type,
            "amount": r.amount,
            "reason": r.reason,
            "is_refunded": r.history_id in refunded_ids,
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