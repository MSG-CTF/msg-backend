"""Time-bounded administrator access to scheduled games, verified by their gateway."""
import logging
import math
import time
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from django.conf import settings
from django.core import signing
from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.throttling import SimpleRateThrottle

from apps.accounts.models import User
from apps.common.permissions import IsAdmin
from apps.common.exceptions import RateLimitUnavailable
from apps.common.response import fail, ok
from .models import KothChallenge

logger = logging.getLogger(__name__)
COOKIE_NAME = "__Host-msg_koth_preview"
SALT = "msg-koth-admin-preview-v1"
MAX_AGE = 300
MAX_SESSION_AGE = 86400


def preview_max_age():
    return max(60, min(MAX_SESSION_AGE, settings.KOTH_ADMIN_PREVIEW_SECONDS))


def preview_seconds_left(grant):
    return max(0, math.ceil(grant["expires_at"] - time.time()))


class PreviewAuthorizeThrottle(SimpleRateThrottle):
    scope = "koth_preview_authorize"
    rate = "600/min"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            raise RateLimitUnavailable() from None


def load_preview_ticket(token, challenge_id, origin):
    if not token or not isinstance(token, str) or len(token) > 2048:
        raise ValueError()
    grant = signing.loads(token, salt=SALT, max_age=preview_max_age())
    if not isinstance(grant, dict) or grant.get("v") != 1:
        raise ValueError()
    parsed_challenge = str(UUID(str(grant["challenge_id"])))
    user_id = str(UUID(str(grant["user_id"])))
    if parsed_challenge != challenge_id or grant["origin"] != origin:
        raise ValueError()
    challenge = KothChallenge.objects.filter(pk=parsed_challenge, status="SCHEDULED").first()
    if (challenge is None or preview_origin(challenge) != origin
            or not User.objects.filter(pk=user_id, role="ADMIN").exists()):
        raise ValueError()
    # A later configuration increase must not extend previously issued tickets.
    max_age = grant.get("max_age", MAX_AGE)
    if type(max_age) is not int or not 60 <= max_age <= MAX_SESSION_AGE:
        raise ValueError()
    expires_at = signing.b62_decode(token.rsplit(":", 2)[1]) + min(max_age, preview_max_age())
    if expires_at <= time.time():
        raise ValueError()
    return challenge, {**grant, "expires_at": expires_at}


def preview_origin(challenge):
    try:
        parsed = urlsplit(challenge.challenge_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/") or parsed.port not in (None, 443)):
            return None
        origin = "https://" + parsed.hostname.lower()
    except (ValueError, TypeError):
        return None
    allowed = getattr(settings, "KOTH_ADMIN_PREVIEW_ORIGINS", [])
    return origin if origin in allowed else None


@never_cache
@api_view(["POST"])
@permission_classes([IsAdmin])
def issue_preview(request, challenge_id):
    if not isinstance(request.data, dict) or request.data:
        return fail("INVALID_REQUEST", "추가 설정 없이 요청해주세요", 400)
    challenge = KothChallenge.objects.filter(pk=challenge_id).first()
    if challenge is None:
        return fail("KOTH_CHALLENGE_NOT_FOUND", "문제를 찾지 못했습니다", 404)
    if challenge.status != "SCHEDULED":
        return fail("INVALID_STATE_TRANSITION", "공개 예정 문제에서만 테스트를 열 수 있습니다", 409)
    origin = preview_origin(challenge)
    if origin is None:
        return fail("KOTH_PREVIEW_UNAVAILABLE", "이 문제의 관리자 테스트 연결이 준비되지 않았습니다", 409)
    max_age = preview_max_age()
    grant = signing.dumps({
        "v": 1, "user_id": str(request.user.pk), "challenge_id": str(challenge.pk),
        "origin": origin, "nonce": uuid4().hex, "max_age": max_age,
    }, salt=SALT, compress=True)
    logger.info("KOTH administrator preview issued", extra={
        "actor_id": str(request.user.pk), "koth_challenge_id": str(challenge.pk),
    })
    response = ok({"preview_url": origin + "/__operator/preview#" + urlencode({"ticket": grant}),
                   "expires_in_seconds": max_age})
    response["Cache-Control"] = "no-store, private"
    response["Referrer-Policy"] = "no-referrer"
    return response


@never_cache
@api_view(["GET", "POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([PreviewAuthorizeThrottle])
def authorize_preview(request):
    # Caddy supplies a fixed challenge and origin. Ordinary access JWTs are never accepted.
    # The launcher sends the fragment by POST; it never enters URL/access logs.
    if request.method == "POST":
        if not isinstance(request.data, dict) or set(request.data) != {"ticket"}:
            return fail("INVALID_REQUEST", "테스트 접속 정보가 올바르지 않습니다", 400)
        supplied = request.data.get("ticket")
        if not isinstance(supplied, str) or not supplied:
            return fail("INVALID_PREVIEW", "테스트 접속 정보가 올바르지 않습니다", 401)
    else:
        supplied = ""
    token = supplied or request.COOKIES.get(COOKIE_NAME, "")
    try:
        challenge, grant = load_preview_ticket(
            token, request.headers.get("X-KOTH-Preview-Challenge"),
            request.headers.get("X-KOTH-Preview-Origin"))
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return fail("INVALID_PREVIEW", "테스트 접속이 만료됐습니다 관리자 화면에서 다시 열어주세요", 401)
    response = HttpResponse(status=204)
    response["Cache-Control"] = "no-store, private"
    if supplied:
        response.set_cookie(COOKIE_NAME, token, max_age=preview_seconds_left(grant), secure=True,
                            httponly=True, samesite="Lax", path="/")
        # A previous native login can redirect '/' to a protected page before
        # the player gets a chance to log in under the new preview grant.
        from .preview_game import GAME_COOKIE, PROFILES

        response.delete_cookie(GAME_COOKIE, path="/", samesite="Lax")
        profile = PROFILES.get(challenge.repository_slug)
        if profile and profile.cookie:
            for name in (profile.cookie, *profile.cookie_aliases):
                response.delete_cookie(name, path="/", samesite="Lax")
    return response
