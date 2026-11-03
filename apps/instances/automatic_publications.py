"""Verified main publications use the same durable preparation gate as admin switches."""
from datetime import timedelta
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.adminpanel.models import AdminEvent
from apps.challenge.models import Challenge
from apps.instances.models import (
    AutomaticPublication, ChallengeRelease, ChallengeRuntimeConfig,
    ChallengeUserFileBundle, ImagePreparationRequest, RuntimeType,
)

ACTOR = "release-poller"


def enabled():
    return settings.AUTO_ACTIVATE_PUBLISHED_RELEASES


def record_verified(artifact, workflow, payload):
    if not enabled() or str(payload.get("challenge_slug", "")).startswith("koth-"):
        return
    # The caller has checked the downloaded bundle against this successful run.
    if (workflow.get("status") != "completed" or workflow.get("conclusion") != "success"
            or workflow.get("event") not in {"push", "workflow_dispatch"}
            or workflow.get("head_branch") != settings.RELEASE_POLL_SOURCE_REF.removeprefix("refs/heads/")
            or workflow.get("repository", {}).get("full_name", "").lower() != settings.RELEASE_POLL_REPO.lower()
            or workflow.get("path", "").split("@", 1)[0] != settings.AUTO_ACTIVATE_RELEASE_WORKFLOW
            or workflow.get("head_sha") != payload.get("source_sha")
            or not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_sha", "")))
            or payload.get("scan_result") != "PASS"):
        return
    images = sorted((c["name"], c["image"]) for c in payload["workload"]["containers"])
    candidates = ChallengeRelease.objects.filter(
        challenge_slug=payload["challenge_slug"], registry_revision=payload["registry_revision"],
        created_by=ACTOR,
    ).prefetch_related("containers")
    release = next((r for r in candidates if sorted((c.name, c.image_ref) for c in r.containers.all()) == images), None)
    if release is None or release.runtime_type != RuntimeType.KUBERNETES:
        return
    with transaction.atomic():
        Challenge.objects.select_for_update().get(pk=release.challenge_id)
        runtime = ChallengeRuntimeConfig.objects.select_for_update().filter(challenge_id=release.challenge_id).first()
        # Initial registration still needs an operator-owned execution profile.
        if runtime is None or runtime.current_release_id is None:
            return
        AutomaticPublication.objects.get_or_create(release=release, defaults={
            "artifact_id": artifact["id"], "run_id": workflow["id"], "source_sha": payload["source_sha"],
            "expected_release_id": runtime.current_release_id, "expected_generation": runtime.activation_generation,
        })


def execution_profile(release):
    return (release.runtime_type, release.architecture, release.isolation_profile, release.healthcheck,
            sorted((c.name, sorted((p["port"], p.get("public", False)) for p in c.ports)) for c in release.containers.all()))


def carry_pending_intents(challenge, previous_release_id, current_release_id, previous_generation, generation, exclude=None):
    """Only automatic transitions may advance another publication's captured fence."""
    rows = AutomaticPublication.objects.filter(
        release__challenge=challenge, state="PENDING",
        expected_release_id=previous_release_id, expected_generation=previous_generation,
    )
    if exclude is not None:
        rows = rows.exclude(pk=exclude)
    rows.update(expected_release_id=current_release_id, expected_generation=generation)


def process_automatic_publications(limit=100):
    if not enabled():
        return 0
    from apps.instances.image_preparations import PreparationError, queue_preparation
    ids = list(AutomaticPublication.objects.filter(state__in=["PENDING", "PREPARING"])
               .order_by("created_at").values_list("pk", flat=True)[:limit])
    processed = 0
    for identifier in ids:
        with transaction.atomic():
            initial = AutomaticPublication.objects.select_related("release").get(pk=identifier)
            challenge = Challenge.objects.select_for_update().get(pk=initial.release.challenge_id)
            runtime = ChallengeRuntimeConfig.objects.select_for_update().get(challenge=challenge)
            row = AutomaticPublication.objects.select_for_update(of=("self",)).select_related("release", "preparation").get(pk=identifier)
            if row.state == "PREPARING":
                status = row.preparation.activation_status if row.preparation_id else "FAILED"
                if status in {"ACTIVATED", "SUPERSEDED", "FAILED"}:
                    row.state = status
                    row.error_code = row.preparation.last_error_code if row.preparation_id else "PREPARATION_MISSING"
                    row.save(update_fields=["state", "error_code", "updated_at"])
                continue
            if row.state != "PENDING":
                continue
            newest = ChallengeRelease.objects.filter(challenge=challenge, created_by=ACTOR).order_by("-registry_revision").first()
            if (newest is None or newest.pk != row.pk or runtime.current_release_id != row.expected_release_id
                    or runtime.activation_generation != row.expected_generation
                    or row.release.registry_revision <= runtime.current_release.registry_revision):
                row.state, row.error_code = "SUPERSEDED", "NEWER_VERSION_OR_OPERATOR_SWITCH"
            elif not settings.IMAGE_PREPARATION_ACTIVATE_WHEN_READY:
                continue
            elif ImagePreparationRequest.objects.filter(
                release__challenge=challenge, activation_status="PENDING",
            ).exclude(pk__in=AutomaticPublication.objects.filter(
                preparation__isnull=False,
            ).values("preparation_id")).exists():
                row.state, row.error_code = "HELD", "MANUAL_ACTIVATION_IN_PROGRESS"
            elif execution_profile(row.release) != execution_profile(runtime.current_release):
                row.state, row.error_code = "HELD", "EXECUTION_PROFILE_CHANGED"
            elif row.release.repository_env.get("missing"):
                row.state, row.error_code = "HELD", "RELEASE_ENV_INCOMPLETE"
            elif not ChallengeUserFileBundle.objects.filter(
                challenge=challenge, registry_revision=row.release.registry_revision, source_sha=row.source_sha,
            ).exclude(present=True, object_key="").exists():
                # File publication and release discovery can arrive in either order.
                row.error_code = "WAITING_FOR_PARTICIPANT_FILES"
                row.save(update_fields=["error_code", "updated_at"])
                continue
            else:
                row.release.approved_at = row.release.approved_at or timezone.now()
                row.release.save(update_fields=["approved_at"])
                try:
                    preparation = queue_preparation(row.release, ACTOR)
                except PreparationError as error:
                    row.state, row.error_code = "HELD", error.code
                else:
                    ImagePreparationRequest.objects.filter(release__challenge=challenge, activation_status="PENDING").update(activation_status="SUPERSEDED")
                    runtime.activation_generation += 1
                    runtime.save(update_fields=["activation_generation", "updated_at"])
                    carry_pending_intents(challenge, runtime.current_release_id, runtime.current_release_id,
                                          runtime.activation_generation - 1, runtime.activation_generation, exclude=row.pk)
                    preparation.activation_status = "PENDING"
                    preparation.activation_generation = runtime.activation_generation
                    preparation.activation_previous_release_id = runtime.current_release_id
                    preparation.activation_reason = f"CI {row.run_id} 성공 발행본 자동 전환"
                    preparation.save()
                    row.state, row.error_code, row.preparation = "PREPARING", "", preparation
                    AdminEvent.objects.create(type=AdminEvent.EventType.CHALLENGE_RELEASE_APPROVED,
                        severity=AdminEvent.Severity.INFO, challenge=challenge, actor=ACTOR,
                        message=f"{challenge.title} revision {row.release.registry_revision} CI 검증 후 자동 준비")
            row.save()
            processed += 1
    return processed


def policy_pending(row):
    return (enabled() and row.created_by == ACTOR
            and AutomaticPublication.objects.filter(preparation=row, state="PREPARING").exists()
            and timezone.now() < row.created_at + timedelta(minutes=30))
