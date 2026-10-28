import base64
import hashlib
import hmac
import secrets

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


def matches_token(raw_token, token_hash):
    return bool(raw_token) and secrets.compare_digest(hash_token(raw_token), token_hash)
