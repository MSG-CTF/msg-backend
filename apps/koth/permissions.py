from django.conf import settings
from rest_framework.permissions import BasePermission

from .exceptions import InvalidInternalToken


class HasInternalToken(BasePermission):
    """/internal/** 전용. KOTH 문제 서버가 X-Internal-Token 헤더로 인증한다 (참가자 JWT와 별개)."""

    def has_permission(self, request, view):
        token = request.headers.get("X-Internal-Token")
        expected = settings.KOTH_INTERNAL_TOKEN
        if not expected or not token or token != expected:
            raise InvalidInternalToken()
        return True
