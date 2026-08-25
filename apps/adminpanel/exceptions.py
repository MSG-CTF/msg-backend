from rest_framework import status

from apps.common.exceptions import APIError


class TeamNotFound(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "TEAM_NOT_FOUND"
    message = "존재하지 않는 팀입니다"


class AlreadyBanned(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "ALREADY_BANNED"
    message = "이미 활동이 정지된 팀입니다"


class NotBanned(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "NOT_BANNED"
    message = "활동 정지 상태가 아닌 팀입니다"

class InvalidAmount(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_AMOUNT"
    message = "조정액은 0이 될 수 없습니다"


class InsufficientMileage(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INSUFFICIENT_MILEAGE"
    message = "회수할 마일리지가 부족합니다"