import datetime
import secrets
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes

from apps.common.exceptions import UserHasNoTeam
from apps.common.jwt import hash_token
from apps.common.permissions import IsAuthenticated, IsNotBanned
from apps.common.response import ok

from .models import MileageHistory, PaymentToken, PaymentTokenStatus

QR_TOKEN_TTL_MINUTES = 5

# Create your views here.

def _num(value):
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value

def _get_team(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()
    return request.user.team


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def team_me(request):
    team = _get_team(request)

    jeopardy_score = team.team_score
    #KOTH 앱이 생기면 SUM(koth_solves.earned_score) 로 교체한다.
    koth_score = Decimal("0")

    members = [
        {
            "user_id": str(m.user_id),
            "nickname": m.nickname,
            "role": m.role,
            "is_leader": m.is_leader,
        }
        for m in team.members.order_by("-is_leader", "nickname")
    ]

    return ok(
        {
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "team_score": _num(jeopardy_score + koth_score),
            "jeopardy_score": _num(jeopardy_score),
            "koth_score": _num(koth_score),
            "mileage": team.mileage,
            "is_banned": team.is_banned,
            "ban_reason": team.ban_reason,
            "members": members,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def mileage_history(request):
    team = _get_team(request)

    rows = MileageHistory.objects.filter(team=team).order_by("-created_at", "-history_id")
    history = [
        {
            "history_id": str(row.history_id),
            "type": row.type,
            "amount": row.amount,
            "reason": row.reason,
            "item_name": row.item_name,
            "is_refunded": row.is_refunded,
            "ref_history_id": str(row.ref_history_id) if row.ref_history_id else None,
            "created_at": row.created_at,
        }
        for row in rows
    ]

    return ok({"mileage": team.mileage, "history": history, "total_count": len(history)})


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsNotBanned])
def qr_token(request):
    team = _get_team(request)

    now = timezone.now().replace(microsecond=0)
    raw_token = f"pt_{secrets.token_urlsafe(16)}"
    expires_at = now + datetime.timedelta(minutes=QR_TOKEN_TTL_MINUTES)

    with transaction.atomic():
        # 기존 ACTIVE 토큰을 먼저 무효화한다.
        # uq_payment_tokens_one_active 제약 때문에 순서를 바꾸면 안 된다.
        old_ids = list(
            PaymentToken.objects.select_for_update()
            .filter(team=team, status=PaymentTokenStatus.ACTIVE)
            .values_list("pk", flat=True)
        )
        if old_ids:
            PaymentToken.objects.filter(pk__in=old_ids).update(
                status=PaymentTokenStatus.INVALIDATED, invalidated_at=now
            )

        new_token = PaymentToken.objects.create(
            team=team,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
        )

        if old_ids:
            PaymentToken.objects.filter(pk__in=old_ids).update(invalidated_by_token=new_token)

    return ok({"payment_token": raw_token, "expires_at": expires_at})