from django.db import DatabaseError
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.common.exceptions import InvalidRequest, UserHasNoTeam
from apps.common.permissions import IsAuthenticated
from apps.common.response import ok

from .exceptions import KothChallengesLoadFailed
from .permissions import HasInternalToken
from .services import (
    get_club_detail,
    get_clubs_list,
    get_internal_teams,
    get_koth_me,
    get_leaderboard,
    get_or_create_team_token,
    verify_team_token,
)


def _get_team(request):
    if request.user.team_id is None:
        raise UserHasNoTeam()
    return request.user.team


class KothClubsView(APIView):
    """GET /api/v1/koth/clubs — 인증 불필요."""

    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        try:
            data = get_clubs_list()
        except DatabaseError:
            raise KothChallengesLoadFailed()
        return ok(data)


class KothClubDetailView(APIView):
    """GET /api/v1/koth/clubs/{club_id} — 인증 불필요."""

    permission_classes = [AllowAny]

    def get(self, request, club_id, *args, **kwargs):
        return ok(get_club_detail(club_id))


class KothMeView(APIView):
    """GET /api/v1/koth/me"""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        team = _get_team(request)
        return ok(get_koth_me(team))


class KothLeaderboardView(APIView):
    """GET /api/v1/koth/leaderboard?koth_challenge_id=..."""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        koth_challenge_id = request.query_params.get("koth_challenge_id")
        return ok(get_leaderboard(koth_challenge_id))


class KothTeamTokenView(APIView):
    """GET /api/v1/koth/team_token"""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        team = _get_team(request)
        token = get_or_create_team_token(team)
        return ok(
            {
                "team_id": str(team.team_id),
                "team_name": team.team_name,
                "team_token": token.token,
                "issued_at": token.issued_at,
            }
        )


class InternalTeamsView(APIView):
    """GET /internal/teams — KOTH 문제 서버 -> 플랫폼, X-Internal-Token 인증."""

    authentication_classes = []
    permission_classes = [HasInternalToken]

    def get(self, request, *args, **kwargs):
        return ok(get_internal_teams())


class InternalTeamTokenVerifyView(APIView):
    """POST /internal/koth/team_tokens/verify — KOTH 문제 서버 -> 플랫폼, X-Internal-Token 인증."""

    authentication_classes = []
    permission_classes = [HasInternalToken]

    def post(self, request, *args, **kwargs):
        koth_challenge_id = request.data.get("koth_challenge_id")
        team_token = request.data.get("team_token")
        if not koth_challenge_id or not team_token:
            raise InvalidRequest("koth_challenge_id와 team_token이 필요합니다")
        return ok(verify_team_token(koth_challenge_id, team_token))
