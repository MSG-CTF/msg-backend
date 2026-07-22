from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

# Create your views here.

class MyTeamView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        team = user.team

        if team is None:
            return Response(
                {
                    'code': 'TEAM_NOT_FOUND',
                    'message': '소속된 팀이 없습니다.',
                    'data': None,
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        members = team.members.all()
        members_data = [
            {
                'userId': m.username,
                'nickname': m.nickname,
                'score': m.score,
            }
            for m in members
        ]

        return Response(
            {
                'code': 'SUCCESS',
                'message': '성공',
                'data': {
                    'teamId': str(team.id),
                    'teamName': team.name,
                    'teamScore': team.team_score,
                    'mileage': team.mileage,
                    'myScore': user.score,
                    'members': members_data,
                },
            },
            status=status.HTTP_200_OK,
        )