import base64
import hashlib
import hmac
import secrets

from django.conf import settings

from apps.common.jwt import hash_token


def build_team_token(team_id):
    """팀 ID와 별도 서버 비밀값에서 재현 가능한 팀 토큰을 만든다."""
    digest = hmac.new(
        settings.KOTH_TEAM_TOKEN_SECRET.encode(), str(team_id).encode(), hashlib.sha256
    ).digest()
    return "koth_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def matches_token(raw_token, token_hash):
    return bool(raw_token) and secrets.compare_digest(hash_token(raw_token), token_hash)
