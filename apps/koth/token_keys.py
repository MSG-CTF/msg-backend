"""Database-pinned KOTH key generations and atomic, auditable cutovers."""

import hashlib
import hmac
import json
import re
import secrets
import uuid

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.adminpanel.models import AdminEvent
from apps.common.exceptions import KothTokenKeyUnavailable
from apps.common.jwt import hash_token
from .models import (
    KothPreviewToken,
    KothTeamToken,
    KothTokenKeyRotation,
    KothTokenKeyState,
)
from .tokens import build_preview_token, build_team_token


class KeyRotationError(ValueError):
    pass


def key_fingerprint():
    # Domain-separated fingerprint for comparison only, never an HMAC signing key.
    return hmac.new(
        settings.KOTH_TEAM_TOKEN_SECRET.encode(),
        b"msgctf:koth-team-token-key:v1",
        hashlib.sha256,
    ).hexdigest()


def current_key_version(*, lock=False):
    try:
        query = KothTokenKeyState.objects
        if lock:
            query = query.select_for_update()
        state = query.get(pk=1)
        if not state.version or not secrets.compare_digest(
            state.fingerprint, key_fingerprint()
        ):
            raise KothTokenKeyUnavailable()
        return state.version
    except (KothTokenKeyState.DoesNotExist, DatabaseError):
        # Do not log the driver exception: connection errors can include credentials.
        raise KothTokenKeyUnavailable() from None


def _validate_receipt(actor, reason, expected_version, operation_id):
    if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,50}", actor):
        raise KeyRotationError(
            "actor must be an operator identifier of at most 50 characters"
        )
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 500
        or not reason.isprintable()
    ):
        raise KeyRotationError(
            "reason must be a nonempty single line of at most 500 characters"
        )
    if any(
        value and value in actor + reason
        for value in [
            settings.KOTH_TEAM_TOKEN_SECRET,
            settings.JWT_SECRET,
            settings.SECRET_KEY,
        ]
    ):
        raise KeyRotationError("Key material must not appear in operation metadata")
    if type(expected_version) is not int or not 0 <= expected_version < 2**63 - 1:
        raise KeyRotationError("expected_version must be a nonnegative 64-bit integer")
    try:
        return uuid.UUID(str(operation_id))
    except (ValueError, TypeError, AttributeError):
        raise KeyRotationError("operation_id must be a UUID") from None


def _adopt_legacy_tokens(version):
    """Initial cutover preserves only legacy hashes proven against this exact key."""
    adopted_team = adopted_preview = 0
    for row in KothTeamToken.objects.filter(
        key_version=0, challenge__isnull=False
    ).iterator():
        expected = hash_token(build_team_token(row.team_id, row.challenge_id))
        if secrets.compare_digest(row.token_hash, expected):
            adopted_team += KothTeamToken.objects.filter(
                pk=row.pk, key_version=0, token_hash=expected
            ).update(key_version=version)
    for row in KothPreviewToken.objects.filter(
        key_version=0, admin__isnull=False
    ).iterator():
        expected = hash_token(build_preview_token(row.admin_id, row.challenge_id))
        if secrets.compare_digest(row.token_hash, expected):
            adopted_preview += KothPreviewToken.objects.filter(
                pk=row.pk, key_version=0, token_hash=expected
            ).update(key_version=version)
    return adopted_team, adopted_preview


def _legacy_key_state():
    """Keep retired TC-622 key IDs effective while adopting its current key."""
    from apps.common.key_lifecycle import key_id
    from apps.instances.models import ServiceKeyRotation, ServiceKeyState

    legacy = ServiceKeyState.objects.select_for_update().filter(pk="koth").first()
    legacy_id = key_id("koth", settings.KOTH_TEAM_TOKEN_SECRET)
    if ServiceKeyRotation.objects.filter(
        purpose="koth", active_key_id=legacy_id
    ).exists() and (legacy is None or legacy.active_key_id != legacy_id):
        raise KeyRotationError(
            "This key was retired by the previous service key authority"
        )
    return legacy, legacy_id


def _synchronize_legacy_authority(legacy, legacy_id, actor, operation_id, activated_at):
    """Pin still-draining TC-622 workers to the same key, in this transaction."""
    from apps.instances.models import ServiceKeyRotation, ServiceKeyState

    if legacy is not None and legacy.active_key_id == legacy_id:
        return
    previous = legacy.active_key_id if legacy else ""
    generation = legacy.generation + 1 if legacy else 1
    ServiceKeyState.objects.update_or_create(
        purpose="koth",
        defaults={
            "active_key_id": legacy_id,
            "generation": generation,
            "activated_at": activated_at,
        },
    )
    ServiceKeyRotation.objects.create(
        purpose="koth",
        previous_key_id=previous,
        active_key_id=legacy_id,
        generation=generation,
        actor=actor,
        reason="TC-613 operation " + str(operation_id),
        reencrypted_rows=0,
    )


def activate_key(*, actor, reason, expected_version, operation_id):
    """Use the key already supplied through the operator's protected environment.

    The singleton lock serializes issuance, verification and concurrent cutovers.
    Retired key fingerprints stay in the ledger, so rollback cannot reactivate one.
    """
    operation_id = _validate_receipt(actor, reason, expected_version, operation_id)
    fingerprint = key_fingerprint()
    with transaction.atomic():
        KothTokenKeyState.objects.get_or_create(pk=1)
        state = KothTokenKeyState.objects.select_for_update().get(pk=1)
        previous = KothTokenKeyRotation.objects.filter(
            operation_id=operation_id
        ).first()
        if previous:
            if (
                previous.fingerprint != fingerprint
                or previous.actor != actor
                or previous.reason != reason
                or previous.previous_version != expected_version
            ):
                raise KeyRotationError(
                    "Operation ID was already used for a different cutover"
                )
            return previous, False
        if state.version != expected_version:
            raise KeyRotationError(
                "Active key version changed; inspect status before retrying"
            )
        if KothTokenKeyRotation.objects.filter(fingerprint=fingerprint).exists():
            raise KeyRotationError(
                "This key was already registered; use a fresh independent key"
            )
        legacy, legacy_id = _legacy_key_state()
        version = state.version + 1
        team_count = KothTeamToken.objects.filter(key_version=state.version).count()
        preview_count = KothPreviewToken.objects.filter(
            key_version=state.version
        ).count()
        adopted_team = adopted_preview = 0
        if state.version == 0:
            adopted_team, adopted_preview = _adopt_legacy_tokens(version)
        activated_at = timezone.now()
        _synchronize_legacy_authority(
            legacy, legacy_id, actor, operation_id, activated_at
        )
        metadata = {
            "service": "KOTH_TEAM_TOKEN_SECRET",
            "operation_id": str(operation_id),
            "previous_version": state.version,
            "version": version,
            "reason": reason,
            "team_token_count": team_count,
            "preview_token_count": preview_count,
            "adopted_team_token_count": adopted_team,
            "adopted_preview_token_count": adopted_preview,
            "activated_at": activated_at.isoformat().replace("+00:00", "Z"),
            "previous_revoked_at": activated_at.isoformat().replace("+00:00", "Z")
            if state.version
            else None,
        }
        event = AdminEvent.objects.create(
            type=AdminEvent.EventType.SERVICE_KEY_ROTATED,
            severity=AdminEvent.Severity.WARNING,
            actor=actor,
            message=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )
        receipt = KothTokenKeyRotation.objects.create(
            operation_id=operation_id,
            version=version,
            fingerprint=fingerprint,
            previous_version=state.version,
            actor=actor,
            reason=reason,
            audit_event=event,
            team_token_count=team_count,
            preview_token_count=preview_count,
            adopted_team_token_count=adopted_team,
            adopted_preview_token_count=adopted_preview,
            activated_at=activated_at,
        )
        state.version, state.fingerprint = version, fingerprint
        state.save(update_fields=["version", "fingerprint"])
    return receipt, True


def receipt_metadata(receipt):
    # Deliberately omit even key fingerprints from command output and admin events.
    return {
        "operation_id": str(receipt.pk),
        "version": receipt.version,
        "previous_version": receipt.previous_version,
        "actor": receipt.actor,
        "reason": receipt.reason,
        "activated_at": receipt.activated_at.isoformat().replace("+00:00", "Z"),
        "previous_revoked_at": receipt.activated_at.isoformat().replace("+00:00", "Z")
        if receipt.previous_version
        else None,
        "audit_event_id": str(receipt.audit_event_id),
        "team_token_count": receipt.team_token_count,
        "preview_token_count": receipt.preview_token_count,
        "adopted_team_token_count": receipt.adopted_team_token_count,
        "adopted_preview_token_count": receipt.adopted_preview_token_count,
    }
