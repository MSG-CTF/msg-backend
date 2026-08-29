from django.shortcuts import render
from decimal import Decimal

from django.db.models import Max
from rest_framework.decorators import api_view

from apps.accounts.models import Team
from apps.challenge.models import Solve
from apps.common.response import ok
from apps.common.utils import num
from apps.ranking.ranking import build_team_ranking


TOP_TEAM_COUNT = 8   
TOP3_COUNT = 3      

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
        if team.last_jeopardy_at is None:
            continue

        team_data.append({
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "jeopardy_score": team.team_score,
            "mileage": team.mileage,
            # KOTH 앱이 생기면 SUM(koth_solves.earned_score)로 수정
            "koth_score": Decimal("0"),
            "jeopardy_solved_at": team.last_jeopardy_at,
            # KOTH 앱이 생기면 MIN(koth_solves.solved_at)로 수정
            "koth_solved_at": None,
        })
    return team_data

def collect_solves(team_id): # 그래프용 solve
    solves = Solve.objects.filter(
        team_id=team_id,
    ).select_related("challenge").order_by("solved_at")

    result = []
    for solve in solves:
        result.append({
            "challenge_id": str(solve.challenge_id),
            "source_type": "JEOPARDY",
            "solved_at": format_datetime(solve.solved_at),
            "points": num(solve.challenge.current_score),
        })
    return result


@api_view(["GET"])
def leaderboard(request):
    rankings = build_team_ranking(collect_team_data(), limit=TOP_TEAM_COUNT)

    teams = []
    for row in rankings:
        teams.append({
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "team_score": num(row["team_score"]),
            "is_top3": row["rank"] <= TOP3_COUNT,
            "solves": collect_solves(row["team_id"]),
        })

    return ok({
        "teams": teams,
        "total_count": len(teams),
    })