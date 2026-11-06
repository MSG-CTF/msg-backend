import logging
import uuid
import time
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge
from apps.common.bans import check_active_team
from apps.common.exceptions import TeamBanned
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
    call_scheduler_request_result,
    call_scheduler_reset,
    call_scheduler_active,
    update_instance_from_scheduler,
    RESETTABLE_INSTANCE_STATUSES,
    create_instance_from_scheduler,
    mark_instance_replaced,
    scheduler_auth_header,
    scheduler_delete_reason,
    serialize_instance,
    validate_scheduler_instance_scope,
)

logger = logging.getLogger(__name__)
BLOCKING_STATUSES = (
    InstanceWriteOperation.Status.STARTED,
    InstanceWriteOperation.Status.RECOVERY_PENDING,
    InstanceWriteOperation.Status.REVIEW_REQUIRED,
)


class ResetSourceClosed(SchedulerError):
    def __init__(self, data):
        super().__init__("RESET_SOURCE_CLEANED", "초기화 대상이 종료됐습니다 새 인스턴스를 생성해주세요", 409)
        self.source_data = data


def replay_unaccepted_reset(operation, user, team, challenge):
    """Only reset has an immutable wire command: the source ID and request ID."""
    if operation.action != InstanceWriteOperation.Action.RESET or not operation.request_id_used:
        raise ValueError("not a replayable reset")
    source = Instance.objects.select_for_update().filter(
        pk=operation.source_instance_id, user=user, team=team,
        challenge=challenge, release_id=operation.release_id,
    ).first()
    if source is None:
        raise ValueError("reset source scope changed")
    try:
        check_active_team(team)
    except TeamBanned as error:
        raise SchedulerError("RESET_OWNER_BANNED", "정지된 팀의 초기화를 재전송할 수 없습니다", 403) from error
    auth = scheduler_auth_header()
    detail = call_scheduler_detail(source, auth)
    if not isinstance(detail, dict) or any(detail.get(k) != v for k, v in {
        "instance_id": str(source.pk), "team_id": str(team.pk),
        "challenge_id": str(challenge.pk),
    }.items()) or ("user_id" in detail and detail["user_id"] != str(user.pk)):
        raise ValueError("reset source response scope changed")
    if detail.get("status") == InstanceStatus.CLEANED:
        try:
            call_scheduler_active(user, auth)
        except SchedulerError as error:
            if error.code == "INSTANCE_NOT_FOUND" and error.status_code == 404:
                raise ResetSourceClosed(detail) from error
            raise
        raise ValueError("another active instance prevents cancellation")
    if detail.get("status") not in RESETTABLE_INSTANCE_STATUSES:
        raise SchedulerError("INSTANCE_REQUEST_NOT_FOUND", "기존 인스턴스의 종료 상태를 확인하고 있습니다", 404)
    call_scheduler_reset(source, auth, request_id=operation.pk)


def serialize_write_result(instance, **options):
    data = serialize_instance(instance, **options)
    if instance is None:
        return data
    operation_id = InstanceWriteOperation.objects.filter(
        user_id=instance.user_id, team_id=instance.team_id,
        challenge_id=instance.challenge_id, release_id=instance.release_id,
        remote_instance_id=instance.pk, request_id_used=True,
        status=InstanceWriteOperation.Status.COMPLETED,
    ).order_by("-created_at", "-operation_id").values_list("pk", flat=True).first()
    if operation_id:
        data["operation_id"] = str(operation_id)
    return data


def recovery_error_data(operation, error):
    if operation.request_id_used and error.status_code >= 500:
        return {"operation_id": str(operation.pk)}
    return None


def require_no_pending_write(user):
    pending = InstanceWriteOperation.objects.filter(
        user_id=user.pk, status__in=BLOCKING_STATUSES
    ).first()
    if pending:
        if pending.request_id_used and pending.status != InstanceWriteOperation.Status.REVIEW_REQUIRED:
            raise SchedulerError(
                "INSTANCE_OPERATION_PENDING", "이전 요청의 결과를 확인하고 있습니다 잠시 후 다시 조회해주세요", 409,
            )
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
        request_id_used=settings.SCHEDULER_REQUEST_RECOVERY_ENABLED,
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
    elif sent and operation.request_id_used and (not isinstance(error, SchedulerError) or error.status_code >= 500):
        status, code = InstanceWriteOperation.Status.RECOVERY_PENDING, "REQUEST_RESULT_PENDING"
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
    if (snapshot.request_id_used and snapshot.status == InstanceWriteOperation.Status.STARTED
            and snapshot.updated_at <= timezone.now() - timedelta(seconds=settings.SCHEDULER_WRITE_TIMEOUT_SECONDS + 5)):
        eligible.append(InstanceWriteOperation.Status.STARTED)
    if review and (snapshot.remote_instance_id or snapshot.request_id_used):
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
                request_data = None
                if operation.remote_instance_id is None and operation.request_id_used:
                    waiting_for_scheduler = True
                    request_data = verified_request_result(operation, release, replay_context=(user, team, challenge))
                    waiting_for_scheduler = False
                    bind_remote_instance(operation, request_data)
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
                    if request_data is not None:
                        data = request_data
                    else:
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
        except ResetSourceClosed as error:
            source = Instance.objects.select_for_update().get(
                pk=operation.source_instance_id, user=user, team=team, challenge=challenge,
            )
            update_instance_from_scheduler(source, error.source_data)
            operation.status = InstanceWriteOperation.Status.REJECTED
            operation.last_error_code = "RESET_SOURCE_CLEANED"
        except SchedulerError as error:
            transient = waiting_for_scheduler and error.status_code >= 500
            not_yet_found = (
                waiting_for_scheduler and operation.request_id_used
                and error.code == "INSTANCE_REQUEST_NOT_FOUND"
                and operation.created_at > timezone.now() - timedelta(seconds=settings.SCHEDULER_REQUEST_RECOVERY_WAIT_SECONDS)
            )
            operation.status = InstanceWriteOperation.Status.REJECTED if error.code == "RESET_OWNER_BANNED" else (
                InstanceWriteOperation.Status.RECOVERY_PENDING
                if transient or not_yet_found
                else InstanceWriteOperation.Status.REVIEW_REQUIRED
            )
            operation.last_error_code = (
                "SCHEDULER_UNAVAILABLE" if transient else "REQUEST_RESULT_NOT_FOUND" if not_yet_found else "REMOTE_STATE_UNCONFIRMED"
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
            update_fields=["status", "attempts", "last_error_code", "remote_instance_id", "source_instance_id", "updated_at"]
        )
        return operation.status


def verified_request_result(operation, release, *, replay_context=None):
    try:
        result = call_scheduler_request_result(operation)
    except SchedulerError as error:
        if (error.code != "INSTANCE_REQUEST_NOT_FOUND" or error.status_code != 404
                or replay_context is None or operation.action != InstanceWriteOperation.Action.RESET):
            raise
        replay_unaccepted_reset(operation, *replay_context)
        result = call_scheduler_request_result(operation)
    expected = {"request_id": str(operation.pk), "user_id": str(operation.user_id), "action": operation.action}
    if not isinstance(result, dict) or any(result.get(k) != v for k, v in expected.items()):
        raise ValueError("request result identity mismatch")
    expected_source = str(operation.source_instance_id) if operation.source_instance_id else None
    if result.get("source_instance_id") != expected_source:
        raise ValueError("request result source mismatch")
    data = result.get("instance")
    if not isinstance(data, dict) or any(data.get(k) != v for k, v in {
        "team_id": str(operation.team_id), "challenge_id": str(operation.challenge_id),
    }.items()) or data.get("status") not in InstanceStatus.values:
        raise ValueError("request result scope mismatch")
    if "user_id" in data and data["user_id"] != str(operation.user_id):
        raise ValueError("request result owner mismatch")
    data = {**data, "user_id": str(operation.user_id), "replaced_instance_id": expected_source}
    expected_release = str(operation.release_id) if operation.release_id else None
    if data.get("release_id") != expected_release:
        # Old non-derived sources may predate Scheduler release metadata.
        # Reset copies that exact source spec; never apply this exception to create.
        legacy_source = (
            operation.action == InstanceWriteOperation.Action.RESET and release is not None
            and not release.derived_from_id and data.get("release_id") is None
            and data.get("registry_revision") == release.registry_revision
            and Instance.objects.filter(pk=operation.source_instance_id, user_id=operation.user_id,
                                        team_id=operation.team_id, challenge_id=operation.challenge_id,
                                        release_id=operation.release_id).exists()
        )
        if not legacy_source:
            raise ValueError("request result release mismatch")
        data["release_id"] = expected_release
    return data


def resume_matching_write(user, *, action=None, challenge_id=None, source_instance_id=None):
    operation = InstanceWriteOperation.objects.filter(
        user_id=user.pk, team_id=user.team_id, request_id_used=True,
    ).order_by("-created_at", "-operation_id").first()
    if operation is None:
        return None
    if operation.status in BLOCKING_STATUSES:
        recover_write(operation.pk)
        operation.refresh_from_db()
    if (operation.status != InstanceWriteOperation.Status.COMPLETED or action is None
            or operation.action != action or str(operation.challenge_id) != str(challenge_id)
            or operation.source_instance_id != source_instance_id):
        return None
    instance = Instance.objects.filter(pk=operation.remote_instance_id, user_id=user.pk,
                                       team_id=user.team_id, challenge_id=operation.challenge_id,
                                       release_id=operation.release_id).first()
    if instance is None:
        return None
    if action == InstanceWriteOperation.Action.CREATE and instance.status not in ACTIVE_INSTANCE_STATUSES:
        return None
    return instance


def recover_pending_writes_once(*, batch_size=10, max_seconds=5):
    cutoff = timezone.now() - timedelta(seconds=settings.SCHEDULER_WRITE_TIMEOUT_SECONDS + 5)
    candidates = InstanceWriteOperation.objects.filter(request_id_used=True).filter(
        Q(status=InstanceWriteOperation.Status.RECOVERY_PENDING)
        | Q(status=InstanceWriteOperation.Status.STARTED, updated_at__lte=cutoff)
    ).order_by("updated_at", "operation_id").values_list("pk", flat=True)[:batch_size]
    started = time.monotonic()
    result = {"checked": 0, "completed": 0, "pending": 0, "review": 0}
    for operation_id in candidates:
        if time.monotonic() - started >= max_seconds:
            break
        status = recover_write(operation_id)
        result["checked"] += 1
        key = "completed" if status == InstanceWriteOperation.Status.COMPLETED else "review" if status == InstanceWriteOperation.Status.REVIEW_REQUIRED else "pending"
        result[key] += 1
    return result
