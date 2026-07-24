from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from apps.teams.models import Team
from .serializers import RankingSerializer


@api_view(['GET'])
def ranking_list(request):
    teams = Team.objects.filter(is_banned=False).order_by('-team_score', 'created_at')

    for index, team in enumerate(teams, start=1):
        team.rank = index

    serializer = RankingSerializer(teams, many=True)
    return Response(serializer.data)