from rest_framework import status

from apps.common.exceptions import APIError


class SignatureNotFound(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "SIGNATURE_NOT_FOUND"
    message = "존재하지 않는 시그니처 문제 ID입니다."


class SignatureAlreadyExists(APIError):
    status_code = status.HTTP_409_CONFLICT
    code = "SIGNATURE_ALREADY_EXISTS"
    message = "해당 클럽의 시그니처 문제가 이미 존재합니다."
