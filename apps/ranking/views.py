from decimal import Decimal
from rest_framework.decorators import api_view, permission_classes
from apps.accounts.models import Team
from apps.common.response import ok
from apps.common.utils import num
from apps.ranking.ranking import build_team_ranking
from apps.common.exceptions import UserHasNoTeam
from apps.common.permissions import IsAuthenticated
from apps.ranking.pagination import parse_pagination
from django.db.models import Max


def collect_team_data():
    teams = Team.objects.filter(is_banned=False).annotate(
        last_jeopardy_at=Max("solves__solved_at"),
    )

    team_data = []
    for team in teams:
        team_data.append({
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "jeopardy_score": team.team_score,
            "mileage": team.mileage,
            "koth_score": Decimal("0"),
            "jeopardy_solved_at": team.last_jeopardy_at,
            "koth_solved_at": None,
        })
    return team_data


@api_view(["GET"])
def team_ranking(request):
    page, size = parse_pagination(
        request.query_params.get("page"),
        request.query_params.get("size"),
    )

    rankings = build_team_ranking(collect_team_data(), limit=None) 

    for row in rankings:                            
        row["team_score"] = num(row["team_score"])
        if row["last_solved_at"] is not None:
            row["last_solved_at"] = row["last_solved_at"].strftime("%Y-%m-%dT%H:%M:%SZ")

    start = (page - 1) * size
    end = start + size

    return ok({
        "rankings": rankings[start:end],
        "total_count": len(rankings),
        "page": page,
        "size": size,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_team_ranking(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()

    my_team_id = str(request.user.team_id)

    rankings = build_team_ranking(collect_team_data(), limit=None) #전체 순위

    for row in rankings:
        if row["team_id"] == my_team_id:
            row["team_score"] = num(row["team_score"])
            return ok(row)

    return ok(None)
        