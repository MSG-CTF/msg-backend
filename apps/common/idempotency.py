import hashlib
import json

from django.db import transaction

from .exceptions import IdempotencyKeyConflict, IdempotencyKeyRequired
from .models import IdempotencyRecord
from .response import ok


def _body_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_idempotent(request, payload, work, message="성공"):
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise IdempotencyKeyRequired()

    request_hash = _body_hash(payload)
    endpoint = request.path

    with transaction.atomic():
        record, created = IdempotencyRecord.objects.get_or_create(
            user=request.user,
            endpoint=endpoint,
            key=key,
            defaults={"request_hash": request_hash},
        )
        if not created:
            if record.request_hash != request_hash:
                raise IdempotencyKeyConflict()
            if record.response_status is None:
                raise IdempotencyKeyConflict("같은 요청을 처리하고 있습니다")
            return ok(record.response_body, message=record.response_message)

        data = work()
        record.response_status = 200
        record.response_message = message
        record.response_body = data
        record.save(update_fields=["response_status", "response_message", "response_body"])

    return ok(data, message=message)