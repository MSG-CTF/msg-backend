from rest_framework.decorators import api_view
from rest_framework.response import Response
from apps.accounts.models import Team
from apps.ranking.ranking import build_team_ranking


@api_view(["GET"])
def team_ranking(request):
    teams = Team.objects.filter(is_banned=False) 

    team_data = []
    for team in teams:
        team_data.append({
            "team_id": str(team.team_id),         
            "team_name": team.team_name,
            "jeopardy_score": team.team_score,
            "koth_score": 0,                       
            "jeopardy_solved_at": None,            
            "koth_solved_at": None,
        })

    rankings = build_team_ranking(team_data)

    return Response({
        "code": "SUCCESS",
        "message": "성공",
        "data": {
            "rankings": rankings,
        },
    })
