from rest_framework.response import Response
from rest_framework.views import APIView

from apps.board.serializers import TeamBoardStateSerializer
from apps.board.services import get_or_create_default_team_state
from apps.teams.serializers import TeamSerializer


class TeamMeView(APIView):
    """GET /api/v1/teams/me for the single-team development mode."""

    def get(self, request, *args, **kwargs):
        team, board_state = get_or_create_default_team_state()

        return Response({
            "code": "SUCCESS",
            "message": "Team lookup succeeded",
            "data": {
                "team": TeamSerializer(team).data,
                "board_state": (
                    TeamBoardStateSerializer(board_state).data
                    if board_state is not None
                    else None
                ),
            },
        })
