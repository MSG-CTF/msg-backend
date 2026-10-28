"""Revalidate operator game sessions without changing challenge application code.

Only the SCHEDULED administrator preview route uses this boundary. Ordinary
released traffic and the game's scoring/solver implementation remain separate.
"""
import json
import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny

from apps.accounts.models import User
from apps.common.exceptions import RateLimitUnavailable
from apps.common.jwt import hash_token
from apps.common.response import fail
from .admin_preview import COOKIE_NAME, MAX_AGE, PreviewAuthorizeThrottle, load_preview_ticket
from .models import KothTeamToken

GAME_COOKIE = "__Host-msg_koth_game"
KEY_PREFIX = "koth:preview-game:v1:"
MAX_BODY = 1024 * 1024
MAX_RESPONSE = 1024 * 1024
NATIVE_TIMEOUT = 8
PERMIT_SECONDS = 12


@dataclass(frozen=True)
class Profile:
    login: str
    kind: str
    cookie: str = ""
    logout: str = ""
    response_token: str = "session_token"


PROFILES = {
    "koth-card-battle-arena": Profile("/api/session", "card", response_token="session_id"),
    "koth-jenga": Profile("/auth", "cookie", cookie="session"),
    "koth-swanopoly": Profile("/api/team/login", "cookie", cookie="swan_sid"),
    "koth-pixel-smuggler": Profile("/api/v1/session", "bearer", logout="/api/v1/session"),
    "koth-grand-tour-duel": Profile("/api/team/session", "cookie", cookie="koth_duel_session", logout="/api/team/session"),
    "koth-dependency-hell": Profile("/api/v1/session", "bearer", logout="/api/v1/session"),
}
PUBLIC_PATHS = {"/", "/healthz", "/favicon.ico", "/config.js", "/api/cards"}


class PreviewDenied(Exception):
    pass


def _get(key):
    try:
        return cache.get(key)
    except Exception:
        raise RateLimitUnavailable() from None


def _delete(key):
    try:
        cache.delete(key)
    except Exception:
        raise RateLimitUnavailable() from None


def _session_key(sid):
    if not isinstance(sid, str) or not 32 <= len(sid) <= 128:
        raise PreviewDenied()
    return KEY_PREFIX + "session:" + hash_token(sid)


def _forget(request):
    sid = request.COOKIES.get(GAME_COOKIE)
    if sid:
        try:
            _delete(_session_key(sid))
        except PreviewDenied:
            pass


def _context(request):
    try:
        challenge, grant = load_preview_ticket(
            request.COOKIES.get(COOKIE_NAME),
            request.headers.get("X-KOTH-Preview-Challenge"),
            request.headers.get("X-KOTH-Preview-Origin"),
        )
        allowed = getattr(settings, "KOTH_PREVIEW_GAME_ADMIN_ALLOWLIST", ())
        if allowed and grant["user_id"] not in allowed:
            raise ValueError()
        return challenge, grant, PROFILES[challenge.repository_slug]
    except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError):
        _forget(request)
        raise PreviewDenied() from None


def _check_origin(request, grant, method):
    if method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("Origin") != grant["origin"]:
        raise PreviewDenied()


def _uri(value):
    if not isinstance(value, str) or len(value) > 4096 or not value.startswith("/"):
        raise PreviewDenied()
    parsed = urlsplit(value)
    decoded = parsed.path
    for _ in range(3):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    else:
        if unquote(decoded) != decoded:
            raise PreviewDenied()
    if (parsed.scheme or parsed.netloc or parsed.fragment or decoded.startswith("//")
            or "\\" in decoded or any(ord(c) < 32 for c in decoded)
            or any(p in {".", ".."} for p in decoded.split("/"))
            or decoded == "/internal" or decoded.startswith("/internal/")
            or decoded == "/__operator" or decoded.startswith("/__operator/")):
        raise PreviewDenied()
    return value


def _json(body):
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise PreviewDenied() from None
    if not isinstance(data, dict):
        raise PreviewDenied()
    return data


def _credential(request, profile, uri, body):
    if profile.kind == "cookie":
        return request.COOKIES.get(profile.cookie, "")
    if profile.kind == "bearer":
        value = request.headers.get("Authorization", "")
        return value[7:] if value.startswith("Bearer ") else ""
    if request.method in {"GET", "HEAD"}:
        return parse_qs(urlsplit(uri).query).get("session_id", [""])[-1]
    value = _json(body).get("session_id")
    return value if isinstance(value, str) else ""


def _authorize(request, challenge, grant, profile, uri, body=b""):
    sid = request.COOKIES.get(GAME_COOKIE)
    key = _session_key(sid)
    stored = _get(key)
    credential = _credential(request, profile, uri, body)
    valid = (isinstance(stored, dict) and credential
             and stored.get("challenge_id") == str(challenge.pk)
             and stored.get("admin_id") == grant["user_id"]
             and stored.get("preview_nonce") == grant["nonce"]
             and secrets.compare_digest(stored.get("native_hash", ""), hash_token(credential)))
    if valid:
        current = KothTeamToken.objects.select_related("team").filter(
            pk=stored.get("token_id"), challenge=challenge, team_id=stored.get("team_id"),
            team__is_banned=False, token_hash=stored.get("token_hash"),
        ).first()
        valid = (current is not None
                 and current.issued_at.isoformat() == stored.get("token_issued_at")
                 and current.team.updated_at.isoformat() == stored.get("team_version"))
    if not valid:
        _delete(key)
        raise PreviewDenied()
    return stored


def permits_token_verification(challenge, token):
    """Permit only a native login currently initiated by a validated preview."""
    record = _get(KEY_PREFIX + "permit:" + str(challenge.pk) + ":" + token.token_hash)
    return (isinstance(record, dict)
            and record.get("challenge_id") == str(challenge.pk)
            and record.get("token_hash") == token.token_hash
            and record.get("expires_at", 0) > time.time()
            and User.objects.filter(pk=record.get("admin_id"), role="ADMIN").exists())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _native_request(request, challenge, uri, body):
    headers = {"Accept": request.headers.get("Accept", "application/json"),
               "Origin": challenge.challenge_url.rstrip("/")}
    for name in ["Content-Type", "Authorization", "X-CSRF-Token"]:
        if name in request.headers:
            headers[name] = request.headers[name]
    cookies = SimpleCookie()
    for name, value in request.COOKIES.items():
        if not name.startswith("__Host-msg_koth_"):
            cookies[name] = value
    if cookies:
        headers["Cookie"] = cookies.output(header="", sep=";").strip()
    req = Request(challenge.challenge_url.rstrip("/") + uri,
                  data=body if request.method not in {"GET", "HEAD"} else None,
                  headers=headers, method=request.method)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=NATIVE_TIMEOUT)
    except HTTPError as exc:
        response = exc
    with response:
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError("native preview response is too large")
        result = HttpResponse(data, status=response.status,
                              content_type=response.headers.get("Content-Type", "application/json"))
        for line in response.headers.get_all("Set-Cookie", []):
            parsed = SimpleCookie(); parsed.load(line)
            for name, value in parsed.items():
                if not name.startswith("__Host-msg_koth_"):
                    result.cookies[name] = value
        return result


def _native_login(request, challenge, grant, profile, uri, body):
    raw = _json(body).get("team_token")
    if not isinstance(raw, str) or not 1 <= len(raw) <= 2048:
        raise PreviewDenied()
    token = KothTeamToken.objects.select_related("team").filter(
        challenge=challenge, token_hash=hash_token(raw), team__is_banned=False,
    ).first()
    if token is None:
        raise PreviewDenied()
    key = KEY_PREFIX + "permit:" + str(challenge.pk) + ":" + token.token_hash
    owner = secrets.token_urlsafe(24)
    record = {"owner": owner, "challenge_id": str(challenge.pk),
              "token_hash": token.token_hash, "admin_id": grant["user_id"],
              "expires_at": min(grant["expires_at"], time.time() + PERMIT_SECONDS)}
    try:
        acquired = cache.add(key, record, timeout=PERMIT_SECONDS)
    except Exception:
        raise RateLimitUnavailable() from None
    if not acquired:
        return fail("KOTH_PREVIEW_LOGIN_BUSY", "다른 인증 요청이 진행 중입니다 잠시 후 다시 시도해 주세요", 409)
    try:
        response = _native_request(request, challenge, uri, body)
    finally:
        current = _get(key)
        if isinstance(current, dict) and current.get("owner") == owner:
            _delete(key)
    if response.status_code != 200:
        return response
    if profile.kind == "cookie":
        credential = response.cookies.get(profile.cookie)
        credential = credential.value if credential else ""
    else:
        credential = _json(response.content).get("data", {}).get(profile.response_token, "")
    if not isinstance(credential, str) or not credential:
        raise ValueError("native login response has no credential")
    # Recheck authority after the native call, before issuing the proxy capability.
    try:
        load_preview_ticket(request.COOKIES.get(COOKIE_NAME), str(challenge.pk), grant["origin"])
    except (signing.BadSignature, ValueError, TypeError, KeyError):
        raise PreviewDenied() from None
    if not KothTeamToken.objects.filter(pk=token.pk, token_hash=token.token_hash, team__is_banned=False).exists():
        raise PreviewDenied()
    sid = secrets.token_urlsafe(32)
    stored = {"challenge_id": str(challenge.pk), "admin_id": grant["user_id"],
              "preview_nonce": grant["nonce"], "team_id": str(token.team_id),
              "token_id": token.pk, "token_hash": token.token_hash,
              "token_issued_at": token.issued_at.isoformat(),
              "team_version": token.team.updated_at.isoformat(),
              "native_hash": hash_token(credential)}
    try:
        cache.set(_session_key(sid), stored, timeout=MAX_AGE)
    except Exception:
        raise RateLimitUnavailable() from None
    response.set_cookie(GAME_COOKIE, sid, max_age=MAX_AGE, secure=True, httponly=True, samesite="Lax", path="/")
    return response


def _denied():
    return fail("INVALID_PREVIEW_GAME_SESSION", "게임 인증이 만료됐습니다 관리자 미리보기에서 다시 로그인해 주세요", 401)


@never_cache
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([PreviewAuthorizeThrottle])
def authorize_game(request):
    try:
        challenge, grant, profile = _context(request)
        method = request.headers.get("X-KOTH-Preview-Method")
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            raise PreviewDenied()
        _check_origin(request, grant, method)
        if profile.kind == "card":
            raise PreviewDenied()
        uri = _uri(request.headers.get("X-KOTH-Preview-URI"))
        _authorize(request, challenge, grant, profile, uri)
    except PreviewDenied:
        return _denied()
    return HttpResponse(status=204)


@never_cache
@api_view(["GET", "HEAD", "POST", "DELETE"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([PreviewAuthorizeThrottle])
def proxy_game(request):
    try:
        challenge, grant, profile = _context(request)
        _check_origin(request, grant, request.method)
        uri = _uri(request.headers.get("X-KOTH-Preview-URI"))
        if request.META.get("CONTENT_LENGTH") and int(request.META["CONTENT_LENGTH"]) > MAX_BODY:
            return fail("REQUEST_TOO_LARGE", "미리보기 요청이 너무 큽니다", 413)
        body = request.body
        if len(body) > MAX_BODY:
            return fail("REQUEST_TOO_LARGE", "미리보기 요청이 너무 큽니다", 413)
        path = urlsplit(uri).path
        if request.method == "POST" and path == profile.login:
            return _native_login(request, challenge, grant, profile, uri, body)
        _authorize(request, challenge, grant, profile, uri, body)
        if not (profile.kind == "card" or request.method == "DELETE" and path == profile.logout):
            raise PreviewDenied()
        response = _native_request(request, challenge, uri, body)
        if request.method == "DELETE" and path == profile.logout:
            _forget(request)
            response.delete_cookie(GAME_COOKIE, path="/", samesite="Lax")
        return response
    except PreviewDenied:
        return _denied()
    except (ValueError, OSError):
        return fail("KOTH_PREVIEW_UPSTREAM_ERROR", "게임 서버 인증을 완료하지 못했습니다 잠시 후 다시 시도해 주세요", 502)
