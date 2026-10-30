import logging
import uuid

from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge
from apps.common.bans import check_active_team
from apps.instances.models import (
    ChallengeRelease,
    Instance,
    InstanceLock,
    InstanceStatus,
    InstanceWriteOperation,
)
from apps.instances.services import (
    ACTIVE_INSTANCE_STATUSES,
    SchedulerError,
    call_scheduler_detail,
    create_instance_from_scheduler,
    mark_instance_replaced,
    scheduler_auth_header,
    scheduler_delete_reason,
    validate_scheduler_instance_scope,
)

logger = logging.getLogger(__name__)
BLOCKING_STATUSES = (
    InstanceWriteOperation.Status.STARTED,
    InstanceWriteOperation.Status.RECOVERY_PENDING,
    InstanceWriteOperation.Status.REVIEW_REQUIRED,
)


def require_no_pending_write(user):
    if InstanceWriteOperation.objects.filter(
        user_id=user.pk, status__in=BLOCKING_STATUSES
    ).exists():
        # 서버 장애(SCHEDULER_UNAVAILABLE)와 달리 기다려도 풀리지 않으므로 따로 알린다
        raise SchedulerError(
            "INSTANCE_REVIEW_REQUIRED",
            "이전 인스턴스 요청을 운영진이 확인해야 합니다 운영진에게 알려주세요",
            409,
        )


def begin_write(*, action, user, team, challenge, release, source_instance_id=None):
    require_no_pending_write(user)
    return InstanceWriteOperation.objects.create(
        action=action,
        user_id=user.pk,
        team_id=team.pk,
        challenge_id=challenge.pk,
        release_id=release.pk if release else None,
        source_instance_id=(
            uuid.UUID(str(source_instance_id)) if source_instance_id else None
        ),
    )


def validate_write_owner(operation):
    if not User.objects.filter(
        pk=operation.user_id, team_id=operation.team_id
    ).exists():
        raise SchedulerError("FORBIDDEN", "권한이 필요합니다", 403)
    check_active_team(Team.objects.filter(pk=operation.team_id).first())


def bind_remote_instance(operation, data):
    expected = {
        "user_id": operation.user_id,
        "team_id": operation.team_id,
        "challenge_id": operation.challenge_id,
        "release_id": operation.release_id,
    }
    try:
        remote_id = uuid.UUID(data["instance_id"])
        source_id = (
            uuid.UUID(data["replaced_instance_id"])
            if data.get("replaced_instance_id")
            else None
        )
        if (
            not remote_id.int
            or remote_id == operation.source_instance_id
            or any(
                key in data and data[key] != (str(value) if value is not None else None)
                for key, value in expected.items()
            )
        ):
            raise ValueError()
        if (
            source_id
            and operation.source_instance_id
            and source_id != operation.source_instance_id
        ):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SchedulerError(
            "SCHEDULER_UNAVAILABLE",
            "원격 인스턴스 작업의 소유 정보를 확인할 수 없습니다.",
            503,
        ) from None
    operation.remote_instance_id = remote_id
    if source_id:
        operation.source_instance_id = source_id
    operation.save(
        update_fields=["remote_instance_id", "source_instance_id", "updated_at"]
    )


def record_write_failure(operation, error, *, sent):
    # The journal was committed before the HTTP call; a failed result transaction cannot erase it.
    if operation.remote_instance_id:
        status = InstanceWriteOperation.Status.RECOVERY_PENDING
        code = (
            "DB_WRITE_FAILED"
            if isinstance(error, DatabaseError)
            else "LOCAL_RESULT_REJECTED"
        )
    elif sent and (not isinstance(error, SchedulerError) or error.status_code >= 500):
        status, code = (
            InstanceWriteOperation.Status.REVIEW_REQUIRED,
            "REMOTE_RESULT_UNKNOWN",
        )
    else:
        status, code = InstanceWriteOperation.Status.REJECTED, "REQUEST_REJECTED"
    try:
        InstanceWriteOperation.objects.exclude(
            status=InstanceWriteOperation.Status.COMPLETED
        ).filter(pk=operation.pk).update(
            status=status,
            remote_instance_id=operation.remote_instance_id,
            source_instance_id=operation.source_instance_id,
            last_error_code=code,
            updated_at=timezone.now(),
        )
    except DatabaseError:
        logger.critical(
            "Instance write journal update unavailable: operation_id=%s remote_instance_id=%s",
            operation.pk,
            operation.remote_instance_id,
        )


def complete_write(operation):
    operation.status = InstanceWriteOperation.Status.COMPLETED
    operation.last_error_code = ""
    operation.save(update_fields=["status", "last_error_code", "updated_at"])


def recover_write(operation_id, *, review=False):
    snapshot = InstanceWriteOperation.objects.get(pk=operation_id)
    eligible = [InstanceWriteOperation.Status.RECOVERY_PENDING]
    if review and snapshot.remote_instance_id:
        eligible.append(InstanceWriteOperation.Status.REVIEW_REQUIRED)
    if snapshot.status not in eligible:
        return snapshot.status
    user = User.objects.filter(pk=snapshot.user_id, team_id=snapshot.team_id).first()
    if user is None:
        InstanceWriteOperation.objects.filter(
            pk=snapshot.pk, status__in=eligible
        ).update(
            status=InstanceWriteOperation.Status.REVIEW_REQUIRED,
            last_error_code="OWNER_CHANGED",
            updated_at=timezone.now(),
        )
        return InstanceWriteOperation.Status.REVIEW_REQUIRED

    with transaction.atomic():
        InstanceLock.objects.select_for_update().get_or_create(user=user)
        operation = InstanceWriteOperation.objects.select_for_update().get(
            pk=snapshot.pk
        )
        if operation.status not in eligible:
            return operation.status
        operation.status = InstanceWriteOperation.Status.RECOVERY_PENDING
        operation.attempts += 1
        waiting_for_scheduler = False
        try:
            with transaction.atomic():
                user.refresh_from_db(fields=["team"])
                if user.team_id != operation.team_id:
                    raise ValueError()
                team = Team.objects.get(pk=operation.team_id)
                challenge = Challenge.objects.get(pk=operation.challenge_id)
                release = (
                    ChallengeRelease.objects.get(
                        pk=operation.release_id, challenge=challenge
                    )
                    if operation.release_id
                    else None
                )
                source = None
                if operation.source_instance_id:
                    source = (
                        Instance.objects.select_for_update()
                        .filter(pk=operation.source_instance_id)
                        .first()
                    )
                    if source is not None:
                        validate_scheduler_instance_scope(
                            source, user, team, source.challenge
                        )
                existing = (
                    Instance.objects.select_for_update()
                    .filter(pk=operation.remote_instance_id)
                    .first()
                )
                if existing is not None:
                    validate_scheduler_instance_scope(existing, user, team, challenge)
                    if existing.release_id != operation.release_id:
                        raise ValueError()
                    if source is not None:
                        if existing.replaced_instance_id not in (None, source.pk):
                            raise ValueError()
                        if existing.replaced_instance_id is None:
                            existing.replaced_instance = source
                            existing.save(update_fields=["replaced_instance"])
                else:
                    probe = Instance(
                        instance_id=operation.remote_instance_id,
                        user=user,
                        team=team,
                        challenge=challenge,
                        release=release,
                    )
                    waiting_for_scheduler = True
                    data = call_scheduler_detail(probe, scheduler_auth_header())
                    waiting_for_scheduler = False
                    expected = {
                        "instance_id": str(operation.remote_instance_id),
                        "team_id": str(team.pk),
                        "challenge_id": str(challenge.pk),
                    }
                    if operation.release_id:
                        expected["release_id"] = str(operation.release_id)
                    if (
                        not isinstance(data, dict)
                        or any(
                            data.get(key) != value for key, value in expected.items()
                        )
                        or data.get("status") not in InstanceStatus.values
                        or ("user_id" in data and data["user_id"] != str(user.pk))
                    ):
                        raise ValueError()
                    reason = scheduler_delete_reason(data)
                    restored = create_instance_from_scheduler(
                        data,
                        user=user,
                        team=team,
                        challenge=challenge,
                        release=release,
                        replaced_instance=source,
                    )
                    if reason is not None:
                        restored.delete_reason = reason
                        restored.save(update_fields=["delete_reason"])
                if source is not None and source.status in ACTIVE_INSTANCE_STATUSES:
                    mark_instance_replaced(source)
        except SchedulerError as error:
            transient = waiting_for_scheduler and error.status_code >= 500
            operation.status = (
                InstanceWriteOperation.Status.RECOVERY_PENDING
                if transient
                else InstanceWriteOperation.Status.REVIEW_REQUIRED
            )
            operation.last_error_code = (
                "SCHEDULER_UNAVAILABLE" if transient else "REMOTE_STATE_UNCONFIRMED"
            )
        except DatabaseError:
            operation.last_error_code = "DB_WRITE_FAILED"
        except (
            ValueError,
            User.DoesNotExist,
            Team.DoesNotExist,
            Challenge.DoesNotExist,
            ChallengeRelease.DoesNotExist,
        ):
            operation.status = InstanceWriteOperation.Status.REVIEW_REQUIRED
            operation.last_error_code = "RECOVERY_SCOPE_MISMATCH"
        else:
            operation.status = InstanceWriteOperation.Status.COMPLETED
            operation.last_error_code = ""
        operation.save(
            update_fields=["status", "attempts", "last_error_code", "updated_at"]
        )
        return operation.status
