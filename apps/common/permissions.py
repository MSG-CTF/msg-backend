from rest_framework.permissions import BasePermission

from apps.common.exceptions import TeamBanned, TokenMissing

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class IsAuthenticated(BasePermission):

    def has_permission(self, request, view):
        if request.user is None:
            raise TokenMissing()
        return True


class IsNotBanned(BasePermission):

    def has_permission(self, request, view):
        if request.method not in WRITE_METHODS:
            return True
        user = request.user
        if user is None:
            raise TokenMissing()
        if user.team_id and user.team.is_banned:
            raise TeamBanned()
        return True