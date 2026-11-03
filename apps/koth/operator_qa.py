"""An explicitly dated operator-login exception for the pre-event rehearsal."""
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .admin_preview import preview_origin


def operator_qa_open(challenge):
    if challenge.status != "ACTIVE" or preview_origin(challenge) is None:
        return False
    try:
        until = parse_datetime(settings.KOTH_OPERATOR_QA_UNTIL)
    except (TypeError, ValueError):
        return False
    return until is not None and timezone.is_aware(until) and timezone.now() < until
