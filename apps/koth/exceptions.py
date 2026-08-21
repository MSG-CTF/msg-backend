from rest_framework import status

from apps.common.exceptions import APIError


class InvalidClubId(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_CLUB_ID"
    message = "club_id 형식이 올바르지 않습니다."


class ClubNotFound(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "CLUB_NOT_FOUND"
    message = "클럽을 찾을 수 없습니다."


class KothChallengeIdRequired(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "KOTH_CHALLENGE_ID_REQUIRED"
    message = "koth_challenge_id가 필요합니다."


class InvalidKothChallengeId(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_KOTH_CHALLENGE_ID"
    message = "koth_challenge_id 형식이 올바르지 않습니다."


class KothChallengeNotFound(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "KOTH_CHALLENGE_NOT_FOUND"
    message = "KOTH 문제를 찾을 수 없습니다."


class KothChallengesLoadFailed(APIError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "KOTH_CHALLENGES_LOAD_FAILED"
    message = "KOTH 문제 목록을 불러오지 못했습니다."


class InvalidInternalToken(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "INVALID_INTERNAL_TOKEN"
    message = "내부 인증값이 올바르지 않습니다."


class TooManyAttempts(APIError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "TOO_MANY_ATTEMPTS"
    message = "잘못된 팀 토큰을 3회 연속 제출했습니다. 30초 후 다시 시도해주세요."

    def __init__(self, retry_after_seconds):
        super().__init__(data={"retry_after_seconds": retry_after_seconds})
