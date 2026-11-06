"""The database owns the active generation; stale workers cannot reactivate keys."""

from contextlib import contextmanager
from functools import wraps
import hashlib
import secrets

from django.conf import settings
from django.db import transaction

from apps.common.exceptions import APIError
from apps.instances.models import ServiceKeyState


class ServiceKeyUnavailable(APIError):
    status_code = 503
    code = "SERVICE_KEY_UNAVAILABLE"
    message = "현재 서비스 키 버전을 확인할 수 없습니다"


def material_for(purpose):
    if purpose == "koth":
        return settings.KOTH_TEAM_TOKEN_SECRET
    if purpose == "runtime":
        keys = settings.RUNTIME_SECRET_ENCRYPTION_KEYS
        return keys[0] if keys else ""
    raise ValueError("Unknown service key purpose")


def key_id(purpose, material):
    return hashlib.sha256((purpose + "\0" + material).encode()).hexdigest()


@contextmanager
def active_service_key(purpose):
    with transaction.atomic():
        if purpose == "koth":
            # KOTH has one authority, including compatibility callers from TC-622.
            from apps.koth.token_keys import current_key_version

            yield current_key_version(lock=True)
            return
        state = ServiceKeyState.objects.select_for_update().filter(pk=purpose).first()
        if state is None:
            if settings.SERVICE_KEY_STATE_REQUIRED:
                raise ServiceKeyUnavailable()
        elif not secrets.compare_digest(
            state.active_key_id, key_id(purpose, material_for(purpose))
        ):
            raise ServiceKeyUnavailable()
        yield state


def service_key_guard(purpose):
    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            with active_service_key(purpose):
                return function(*args, **kwargs)

        return guarded

    return decorate
