from django.utils import timezone

from apps.instances.models import Instance, InstanceStatus
from apps.instances.services import isoformat_z

MAX_OBSERVATION_AGE_SECONDS = 60
OBSERVATION_ERRORS = {
    "NOT_FOUND",
    "UNAUTHORIZED",
    "FORBIDDEN",
    "UNAVAILABLE",
    "INVALID_RESPONSE",
}


def record_scheduler_observation(snapshot, *, status=None, error_code=""):
    if error_code:
        if error_code not in OBSERVATION_ERRORS or status is not None:
            raise ValueError("Invalid Scheduler observation error")
    elif status not in InstanceStatus.values:
        raise ValueError("Invalid Scheduler observation status")
    # Observation writes must not change lifecycle age or overwrite a changed owner/state.
    return Instance.objects.filter(
        pk=snapshot.pk,
        updated_at=snapshot.updated_at,
        status=snapshot.status,
        user_id=snapshot.user_id,
        team_id=snapshot.team_id,
        challenge_id=snapshot.challenge_id,
        release_id=snapshot.release_id,
    ).update(
        scheduler_status=status,
        scheduler_checked_at=timezone.now(),
        scheduler_error_code=error_code,
    )


def serialize_scheduler_observation(instance, now):
    checked_at = instance.scheduler_checked_at
    check_status = "NOT_CHECKED"
    if checked_at is not None:
        age = (now - checked_at).total_seconds()
        if (
            age < 0
            or age > MAX_OBSERVATION_AGE_SECONDS
            or instance.updated_at > checked_at
        ):
            check_status = "STALE"
        elif instance.scheduler_error_code:
            check_status = "ERROR"
        else:
            check_status = "OK"
    return {
        "status": instance.scheduler_status,
        "checked_at": isoformat_z(checked_at),
        "check_status": check_status,
        "error_code": instance.scheduler_error_code or None,
        "status_mismatch": (
            instance.status != instance.scheduler_status
            if check_status == "OK"
            else None
        ),
    }
