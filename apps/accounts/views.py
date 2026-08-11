import jwt
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes

from apps.common.exceptions import InvalidRequest
from apps.common.jwt import (
    REFRESH,
    decode_token,
    hash_token,
    issue_access_token,
    issue_refresh_token,
)
from apps.common.permissions import IsAuthenticated
from apps.common.response import ok

from .exceptions import (
    InvalidCredentials,
    RefreshTokenExpired,
    RefreshTokenInvalid,
    RefreshTokenNotFound,
)
from .models import RefreshToken, User
from .serializers import LoginSerializer, RefreshTokenSerializer


@api_view(["POST"])
def login(request):
    """POST /api/v1/auth/login — 밴된 팀도 허용한다 (규약 「밴 처리」 예외)."""
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    login_id = serializer.validated_data["login_id"]
    password = serializer.validated_data["password"]

    user = User.objects.select_related("team").filter(login_id=login_id).first()
    if user is None:
        # 계정이 없어도 해싱을 한 번 수행해 응답 시간을 맞춘다.
        # 없는 계정만 빠르게 실패하면 응답 시간으로 계정 존재 여부가 드러난다.
        User().set_password(password)
        raise InvalidCredentials()

    if not user.check_password(password):
        raise InvalidCredentials()

    # 이 유저의 만료된 refresh_token 행을 정리한다.
    # 로그아웃 시에만 삭제되므로, 그냥 두면 만료된 행이 계속 쌓인다.
    RefreshToken.objects.filter(user=user, expires_at__lt=timezone.now()).delete()

    access_token = issue_access_token(user)
    refresh_token, expires_at = issue_refresh_token(user)
    RefreshToken.objects.create(
        user=user,
        token_hash=hash_token(refresh_token),
        expires_at=expires_at,
    )

    return ok(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "role": user.role,
            "is_leader": user.is_leader,
            "nickname": user.nickname,
            "team_id": str(user.team_id) if user.team_id else None,
            "team_name": user.team.team_name if user.team_id else None,
            "user_id": str(user.user_id),
        },
        message="로그인 성공",
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    """GET /api/v1/auth/me — 로그인 상태 확인."""
    user = request.user
    return ok(
        {
            "user_id": str(user.user_id),
            "nickname": user.nickname,
            "is_leader": user.is_leader,
            "team_id": str(user.team_id) if user.team_id else None,
            "team_name": user.team.team_name if user.team_id else None,
            "role": user.role,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    """POST /api/v1/auth/logout — refresh_token 행을 삭제해 재발급을 막는다."""
    serializer = RefreshTokenSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    RefreshToken.objects.filter(
        user=request.user,
        token_hash=hash_token(serializer.validated_data["refresh_token"]),
    ).delete()

    return ok(None, message="로그아웃 성공")


@api_view(["POST"])
def refresh(request):
    """POST /api/v1/auth/refresh — access_token 만 재발급한다 (밴 예외 경로)."""
    serializer = RefreshTokenSerializer(data=request.data)
    if not serializer.is_valid():
        raise InvalidRequest("refresh_token이 필요합니다")

    raw = serializer.validated_data["refresh_token"]

    try:
        decode_token(raw, REFRESH)
    except jwt.ExpiredSignatureError:
        raise RefreshTokenExpired()
    except jwt.InvalidTokenError:
        raise RefreshTokenInvalid()

    row = (
        RefreshToken.objects.select_related("user", "user__team")
        .filter(token_hash=hash_token(raw))
        .first()
    )
    if row is None:
        # 서명은 유효한데 DB에 없다 = 로그아웃됐거나 강제 폐기됨
        raise RefreshTokenNotFound()

    return ok(
        {"access_token": issue_access_token(row.user)},
        message="토큰이 재발급되었습니다",
    )