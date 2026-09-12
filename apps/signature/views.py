from django.db.models import Count, Exists, OuterRef
from rest_framework.decorators import api_view, permission_classes

from apps.common.exceptions import UserHasNoTeam
from apps.common.permissions import IsAuthenticated
from apps.common.response import ok
from apps.common.utils import num

from .exceptions import SignatureNotFound
from .models import SignatureChallenge, SignatureSolve


def _request_team(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()
    return request.user.team


def _isoformat_z(value):
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _base_queryset(team):
    team_solves = SignatureSolve.objects.filter(
        team=team,
        challenge_id=OuterRef("pk"),
    )
    return (
        SignatureChallenge.objects.filter(is_published=True)
        .select_related("club")
        .annotate(
            is_solved=Exists(team_solves),
            solved_team_count=Count("solves", distinct=True),
        )
        .order_by("club__name", "signature_id")
    )


def _serialize_summary(challenge):
    return {
        "signature_id": str(challenge.signature_id),
        "club_id": str(challenge.club_id),
        "club_name": challenge.club.name,
        "title": challenge.title,
        "score": num(challenge.score),
        "is_solved": challenge.is_solved,
        "solved_team_count": challenge.solved_team_count,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def signature_list(request):
    team = _request_team(request)
    rows = [_serialize_summary(challenge) for challenge in _base_queryset(team)]
    return ok(
        {"signatures": rows, "total_count": len(rows)},
        message="시그니처 문제 목록 조회 성공",
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def signature_detail(request, signature_id):
    team = _request_team(request)
    try:
        challenge = _base_queryset(team).get(pk=signature_id)
    except SignatureChallenge.DoesNotExist:
        raise SignatureNotFound()

    solve = SignatureSolve.objects.filter(
        team=team,
        challenge=challenge,
    ).only("solved_at").first()
    data = _serialize_summary(challenge)
    data.update(
        {
            "description": challenge.description,
            "solved_at": _isoformat_z(solve.solved_at) if solve else None,
        }
    )
    return ok(data, message="시그니처 문제 상세 조회 성공")
