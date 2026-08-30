from decimal import Decimal
from django.db.models import Max, Min, OuterRef, Subquery, Sum
from rest_framework.decorators import api_view
from apps.accounts.models import Team
from apps.challenge.models import Solve
from apps.common.response import ok
from apps.common.utils import num
from apps.koth.models import KothSolve
from apps.ranking.ranking import build_team_ranking

TOP_TEAM_COUNT = 8
TOP3_COUNT = 3


def format_datetime(value):
    if value is None:
        return None
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_team_data():
    koth_score_sq = KothSolve.objects.filter(
        team=OuterRef("pk"),
    ).values("team").annotate(total=Sum("earned_score")).values("total")

    koth_first_sq = KothSolve.objects.filter(
        team=OuterRef("pk"),
        solved_at__isnull=False,
    ).values("team").annotate(first=Min("solved_at")).values("first")

    teams = Team.objects.filter(is_banned=False).annotate(
        jeopardy_total=Sum("solves__challenge__current_score"),
        last_jeopardy_at=Max("solves__solved_at"),
        koth_total=Subquery(koth_score_sq),
        first_koth_at=Subquery(koth_first_sq),
    )

    team_data = []
    for team in teams:
        if team.last_jeopardy_at is None and team.first_koth_at is None:
            continue

        team_data.append({
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "jeopardy_score": team.jeopardy_total or Decimal("0"),
            "mileage": team.mileage,
            "koth_score": team.koth_total or Decimal("0"),
            "jeopardy_solved_at": team.last_jeopardy_at,
            "koth_solved_at": team.first_koth_at,
        })
    return team_data


def collect_solves(team_id):
    result = []

    jeopardy = Solve.objects.filter(
        team_id=team_id,
    ).select_related("challenge")

    for solve in jeopardy:
        result.append({
            "challenge_id": str(solve.challenge_id),
            "source_type": "JEOPARDY",
            "solved_at": format_datetime(solve.solved_at),
            "points": num(solve.challenge.current_score),
        })

    koth = KothSolve.objects.filter(
        team_id=team_id,
        solved_at__isnull=False,
    )

    for solve in koth:
        result.append({
            "challenge_id": str(solve.challenge_id),
            "source_type": "KOTH",
            "solved_at": format_datetime(solve.solved_at),
            "points": num(solve.earned_score),
        })

    result.sort(key=lambda row: row["solved_at"])
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