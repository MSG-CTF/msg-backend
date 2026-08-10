from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response


class APIError(APIException):
    """공통 규약 봉투로 내려갈 에러의 기본 클래스."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_REQUEST"
    message = "요청 값이 올바르지 않습니다"

    def __init__(self, message=None, data=None):
        self.message = message or self.message
        self.data = data
        super().__init__(detail=self.message)


class TokenMissing(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "TOKEN_MISSING"
    message = "로그인이 필요합니다"


class TokenExpired(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "TOKEN_EXPIRED"
    message = "세션이 만료되었습니다"


class TokenInvalid(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "TOKEN_INVALID"
    message = "유효하지 않은 인증 정보입니다"


class Forbidden(APIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"
    message = "권한이 필요합니다"


class TeamBanned(APIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "TEAM_BANNED"
    message = "활동이 정지된 팀입니다"


class UserHasNoTeam(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "USER_HAS_NO_TEAM"
    message = "소속된 팀이 없습니다"


class InvalidRequest(APIError):
    pass


class InternalError(APIError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "INTERNAL_ERROR"
    message = "서버 오류가 발생했습니다"


def envelope_exception_handler(exc, context):

    from rest_framework.views import exception_handler as drf_exception_handler

    if isinstance(exc, APIError):
        return Response(
            {"code": exc.code, "message": exc.message, "data": exc.data},
            status=exc.status_code,
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    fallback = {
        401: ("TOKEN_MISSING", "로그인이 필요합니다"),
        403: ("FORBIDDEN", "권한이 필요합니다"),
        404: ("NOT_FOUND", "대상을 찾을 수 없습니다"),
        405: ("METHOD_NOT_ALLOWED", "허용되지 않은 메서드입니다"),
    }
    code, message = fallback.get(
        response.status_code, ("INVALID_REQUEST", "요청 값이 올바르지 않습니다")
    )
    response.data = {"code": code, "message": message, "data": None}
    return response