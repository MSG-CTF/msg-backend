import base64
import hashlib
import hmac
import secrets
import uuid

from django.conf import settings

from apps.common.jwt import hash_token


def build_team_token(team_id, challenge_id):
    """Derive one stable token for each team and KOTH challenge pair."""
    digest = hmac.new(
        settings.KOTH_TEAM_TOKEN_SECRET.encode(),
        f"{team_id}:{challenge_id}".encode(),
        hashlib.sha256,
    ).digest()
    return "koth_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


PREVIEW_TEAM_NAME = "운영 테스트"


def build_preview_token(admin_id, challenge_id):
    """관리자 게임 테스트 토큰. 팀 토큰과 같은 모양이지만 다른 값에서 만들어 겹치지 않는다."""
    digest = hmac.new(
        settings.KOTH_TEAM_TOKEN_SECRET.encode(),
        f"preview:{admin_id}:{challenge_id}".encode(),
        hashlib.sha256,
    ).digest()
    return "koth_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def preview_team_id(admin_id):
    """게임에 보이는 관리자 테스트 팀 ID. 실제 팀(uuid4)과 겹치지 않는 고정 값이다."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"msgctf:koth-preview-team:{admin_id}")


def matches_token(raw_token, token_hash):
    return bool(raw_token) and secrets.compare_digest(hash_token(raw_token), token_hash)
