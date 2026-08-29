from decimal import Decimal
from rest_framework.decorators import api_view, permission_classes
from apps.accounts.models import Team
from apps.common.response import ok
from apps.common.utils import num
from apps.ranking.ranking import build_team_ranking, build_member_ranking
from apps.common.exceptions import UserHasNoTeam
from apps.common.permissions import IsAuthenticated
from apps.ranking.pagination import parse_pagination
from django.db.models import Max, Count, Sum
from apps.accounts.models import Team, User

def format_datetime(value):
    if value is None:
        return None
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        row["last_solved_at"] = format_datetime(row["last_solved_at"])

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

    rankings = build_team_ranking(collect_team_data(), limit=None)  

    for row in rankings:
        if row["team_id"] == my_team_id:
            row["team_score"] = num(row["team_score"])
            row["last_solved_at"] = format_datetime(row["last_solved_at"])
            return ok(row)

    return ok(None)

def collect_member_data():
    users = User.objects.filter(
        team__isnull=False,
        team__is_banned=False,
    ).select_related("team").annotate(
        score_sum=Sum("solves__challenge__current_score"),
        solve_count=Count("solves"),
        last_at=Max("solves__solved_at"),
    )

    member_data = []
    for user in users:
        member_data.append({
            "user_id": str(user.user_id),
            "nickname": user.nickname,
            "team_id": str(user.team_id),
            "team_name": user.team.team_name,
            "user_score": user.score_sum or 0,   
            "solved_count": user.solve_count,
            "last_solved_at": user.last_at,
        })
    return member_data

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def member_ranking(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()

    my_user_id = str(request.user.user_id)

    rankings = build_member_ranking(collect_member_data())

    for row in rankings:
        if row["user_id"] == my_user_id:
            row["user_score"] = num(row["user_score"])
            row["last_solved_at"] = format_datetime(row["last_solved_at"])
            return ok(row)

    return ok(None)