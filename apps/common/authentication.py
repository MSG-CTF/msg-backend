import jwt
from rest_framework.authentication import BaseAuthentication

from apps.common.exceptions import TokenExpired, TokenInvalid
from apps.common.jwt import ACCESS, decode_token


class JWTAuthentication(BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header:
            return None                      # 토큰 없음 → 권한 클래스가 판정
        if not header.startswith(f"{self.keyword} "):
            raise TokenInvalid()

        raw = header[len(self.keyword) + 1 :].strip()
        try:
            payload = decode_token(raw, ACCESS)
        except jwt.ExpiredSignatureError:
            raise TokenExpired()
        except jwt.InvalidTokenError:
            raise TokenInvalid()

        from apps.accounts.models import User

        try:
            user = User.objects.select_related("team").get(pk=payload["sub"])
        except (User.DoesNotExist, ValueError, KeyError):
            raise TokenInvalid()

        return (user, payload)

    def authenticate_header(self, request):
        return self.keyword