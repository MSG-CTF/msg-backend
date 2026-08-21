from rest_framework.permissions import BasePermission

from apps.common.exceptions import TokenMissing, UserHasNoTeam

from .exceptions import NotTeamLeader


class IsTeamLeader(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if user is None:
            raise TokenMissing()
        if user.team_id is None:
            raise UserHasNoTeam()
        if not user.is_leader:
            raise NotTeamLeader()
        return True
