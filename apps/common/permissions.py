from rest_framework.permissions import BasePermission

from apps.common.exceptions import TokenMissing


class IsAuthenticated(BasePermission):
    def has_permission(self, request, view):
        if request.user is None:
            raise TokenMissing()
        return True