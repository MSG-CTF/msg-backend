import hashlib
import hmac
import io
import json
from pathlib import Path

from django.db import IntegrityError
from django.http import FileResponse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.parsers import BaseParser
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.challenge.checker_auth import (
    authenticated_submission,
    checker_configuration,
    mint_job_token,
)
from apps.challenge.checker_events import apply_event
from apps.challenge.exe_submissions import (
    ExeSubmissionError,
    MAX_PAYLOAD_BYTES,
    private_payload_storage,
)
from apps.challenge.models import ExeSubmission
from apps.common.response import ok


class EventJSONParser(BaseParser):
    media_type = "application/json"

    def parse(self, stream, media_type=None, parser_context=None):
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ExeSubmissionError("EVENT_TOO_LARGE", "실행 결과가 너무 큽니다", 413)
        try:
            return json.loads(
                raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
            )
        except (ValueError, UnicodeError, RecursionError):
            raise ExeSubmissionError(
                "INVALID_EVENT", "실행 결과 형식을 확인해주세요"
            ) from None


@method_decorator(never_cache, name="dispatch")
class CheckerView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]


class CheckerPayloadView(CheckerView):
    def get(self, request, submission_id):
        row = authenticated_submission(request, submission_id, file_access=True)
        storage = private_payload_storage()
        try:
            path = Path(storage.path(row.payload_key)).resolve()
            if Path(storage.location).resolve() not in path.parents:
                raise OSError()
            with path.open("rb") as source:
                payload = source.read(MAX_PAYLOAD_BYTES + 1)
            if (
                len(payload) != row.payload_size
                or hashlib.sha256(payload).hexdigest() != row.payload_sha256
            ):
                raise OSError()
        except (OSError, ValueError):
            raise ExeSubmissionError(
                "PAYLOAD_UNAVAILABLE", "실행 파일을 확인할 수 없습니다", 503
            ) from None
        response = FileResponse(
            io.BytesIO(payload),
            as_attachment=True,
            filename="payload.exe",
            content_type="application/octet-stream",
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response


class CheckerSecretView(CheckerView):
    def get(self, request, submission_id):
        authenticated_submission(request, submission_id)
        raise ExeSubmissionError(
            "TOKEN_SCOPE_DENIED", "실행 작업 토큰으로 비밀값을 조회할 수 없습니다", 403
        )


class CheckerEventsView(CheckerView):
    parser_classes = [EventJSONParser]

    def post(self, request, submission_id):
        row = authenticated_submission(request, submission_id)
        try:
            return ok(apply_event(row, request.data))
        except IntegrityError:
            raise ExeSubmissionError(
                "EVENT_CONFLICT", "이미 다른 실행에서 사용한 결과입니다", 409
            ) from None


class CheckerTokenRefreshView(CheckerView):
    parser_classes = [EventJSONParser]

    def post(self, request, submission_id):
        row = ExeSubmission.objects.filter(pk=submission_id).first()
        if (
            row is None
            or row.config_snapshot.get("assignment_mode") != "broker_pool"
            or not row.lease_id
            or not row.worker_id
            or row.lease_released_at is not None
            or row.cleanup_complete is True
        ):
            raise ExeSubmissionError(
                "SUBMISSION_NOT_FOUND", "실행 요청을 찾지 못했습니다", 404
            )
        header = request.headers.get("Authorization", "")
        _, service_token, _ = checker_configuration(row.worker_id, broker_pool=True)
        if (
            not header.startswith("Bearer ")
            or len(header) > 4096
            or not header.isascii()
            or not hmac.compare_digest(
                header[7:].encode("utf-8"), service_token.encode("utf-8")
            )
        ):
            raise ExeSubmissionError("TOKEN_INVALID", "체커 인증 정보를 확인해주세요", 401)
        if (
            not isinstance(request.data, dict)
            or set(request.data) != {"assignment_id", "payload_sha256"}
            or request.data["assignment_id"] != str(row.lease_id)
            or not isinstance(request.data["payload_sha256"], str)
            or not request.data["payload_sha256"].isascii()
            or not hmac.compare_digest(
                request.data["payload_sha256"], row.payload_sha256,
            )
        ):
            raise ExeSubmissionError("INVALID_REQUEST", "실행 배정과 파일을 확인해주세요", 400)
        return ok(
            {
                "submission_id": str(row.pk),
                "assignment_id": str(row.lease_id),
                "job_token": mint_job_token(row),
            }
        )
