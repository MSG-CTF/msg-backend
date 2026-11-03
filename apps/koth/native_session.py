"""Validate the original Jenga cookie at the gateway; leave the game untouched."""
import hashlib
import json
import zlib
from uuid import UUID

from django.core.cache import cache
from django.db import DatabaseError
from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from itsdangerous import BadData, TimestampSigner
from itsdangerous.encoding import base64_decode
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny

from apps.common.response import fail
from apps.instances.runtime_secrets import RuntimeSecretUnavailable
from .environment import decode_settings
from .models import KothChallenge, KothEnvironmentConfig, KothTeamToken

MAX_AGE = 31 * 24 * 60 * 60  # Flask's original cookie lifetime
MAX_COOKIE = 4096
PREFIX = "koth:jenga-native:v1:"


class InvalidSession(Exception):
    pass


class GuardUnavailable(Exception):
    pass


@sensitive_variables()
def cookie_identity(raw, secret):
    if not isinstance(raw, str) or not 1 <= len(raw) <= MAX_COOKIE:
        raise InvalidSession()
    try:
        signer = TimestampSigner(secret, salt="cookie-session", key_derivation="hmac", digest_method=hashlib.sha1)
        payload, issued_at = signer.unsign(raw, max_age=MAX_AGE, return_timestamp=True)
        compressed = payload.startswith(b".")
        payload = base64_decode(payload[1:] if compressed else payload)
        if compressed:
            inflater = zlib.decompressobj()
            payload = inflater.decompress(payload, MAX_COOKIE + 1)
            if inflater.unconsumed_tail or inflater.unused_data or not inflater.eof:
                raise InvalidSession()
        if len(payload) > MAX_COOKIE:
            raise InvalidSession()
        data = json.loads(payload)
        tid = data.get("team_id") if isinstance(data, dict) else None
        if not isinstance(tid, str) or str(UUID(tid)) != tid:
            raise InvalidSession()
        return tid, int(issued_at.timestamp())
    except (BadData, ValueError, TypeError, UnicodeError, zlib.error, OverflowError):
        raise InvalidSession() from None


def _cache(method, *args, **kwargs):
    try:
        return getattr(cache, method)(*args, **kwargs)
    except Exception:
        raise GuardUnavailable() from None


@sensitive_variables()
def _check(request):
    cid = request.headers.get("X-KOTH-Challenge", "")
    try:
        if str(UUID(cid)) != cid:
            raise ValueError()
    except (ValueError, TypeError):
        raise InvalidSession() from None
    challenge = KothChallenge.objects.filter(pk=cid, repository_slug="koth-jenga", status="ACTIVE").first()
    if challenge is None or request.headers.get("X-KOTH-Origin") != challenge.challenge_url.rstrip("/"):
        raise InvalidSession()
    method = request.headers.get("X-KOTH-Request-Method", "GET")
    if method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("Origin") != challenge.challenge_url.rstrip("/"):
        raise InvalidSession()
    cfg = KothEnvironmentConfig.objects.select_related("applied_version").filter(challenge=challenge).first()
    if cfg is None or cfg.applied_version is None:
        raise GuardUnavailable()
    values = decode_settings(cfg.applied_version)
    containers = [c for c in values["containers"] if c["name"] == "jenga"]
    entry = containers[0]["entries"].get("SESSION_SECRET") if len(containers) == 1 else None
    if not entry or entry.get("kind") != "secret" or not entry.get("value"):
        raise GuardUnavailable()
    raw = request.COOKIES.get("session", "")
    tid, issued_at = cookie_identity(raw, entry["value"])
    key = PREFIX + cid + ":" + hashlib.sha256(raw.encode()).hexdigest()
    previous = _cache("get", key)
    if previous == {"revoked": True}:
        raise InvalidSession()
    current = KothTeamToken.objects.select_related("team").filter(challenge=challenge, team_id=tid).first()
    if current is None or current.team.is_banned or issued_at < int(current.issued_at.timestamp()):
        _cache("set", key, {"revoked": True}, timeout=MAX_AGE)
        raise InvalidSession()
    record = {"id": current.pk, "hash": current.token_hash, "issued": current.issued_at.isoformat()}
    if previous is None:
        _cache("add", key, record, timeout=MAX_AGE)
        previous = _cache("get", key)
    if previous != record:
        _cache("set", key, {"revoked": True}, timeout=MAX_AGE)
        raise InvalidSession()


@never_cache
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def authorize_jenga(request):
    try:
        _check(request)
    except InvalidSession:
        return fail("TOKEN_INVALID", "팀 인증을 다시 진행해주세요", status=401)
    except (GuardUnavailable, RuntimeSecretUnavailable, DatabaseError):
        return fail("AUTH_UNAVAILABLE", "인증 상태를 확인하지 못했습니다", status=503)
    return HttpResponse(status=204)
