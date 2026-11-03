import copy
from datetime import timedelta
from io import BytesIO, StringIO
from unittest.mock import patch
from urllib.error import HTTPError

from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.adminpanel.models import AdminEvent
from apps.challenge.models import Challenge
from apps.instances.automatic_publications import record_verified, process_automatic_publications
from apps.instances.image_preparations import process_preparation
from apps.instances.models import AutomaticPublication, ChallengeRelease, ChallengeRuntimeConfig, ChallengeUserFileBundle, ImagePreparationRequest, ReleaseContainer
from apps.instances.tests_image_preparations import CONFIG, accepted, detail
from apps.instances.tests_staged_activation import readiness

SHA = "a" * 40


@override_settings(**CONFIG, IMAGE_PREPARATION_ACTIVATE_WHEN_READY=True, AUTO_ACTIVATE_PUBLISHED_RELEASES=True,
                   RELEASE_POLL_SOURCE_REF="refs/heads/main", RELEASE_POLL_REPO="MSG-CTF/2026_MSG_CTF",
                   AUTO_ACTIVATE_RELEASE_WORKFLOW=".github/workflows/challenge-validation.yml")
class AutomaticPublicationTests(TransactionTestCase):
    def setUp(self):
        self.challenge = Challenge.objects.create(title="Publication fixture", category="WEB", difficulty="EASY", score=500, flag_hash="unused")
        self.old = self.make_release(1)
        self.old.approved_at = timezone.now(); self.old.save()
        self.config = ChallengeRuntimeConfig.objects.create(challenge=self.challenge, current_release=self.old)
        self.new = self.make_release(2)

    def make_release(self, revision):
        row = ChallengeRelease.objects.create(challenge=self.challenge, challenge_slug="web-demo", version=revision, registry_revision=revision,
            isolation_profile="WEB", cpu_millicores=500, memory_mib=512, ephemeral_storage_mib=1024,
            created_by="release-poller", source_ref="refs/heads/main", repository_env={"missing": [], "revision": SHA})
        ReleaseContainer.objects.create(release=row, name="app", image_ref="ghcr.io/msg-ctf/challenges/web-demo/app@sha256:" + str(revision)*64,
            ports=[{"port": 8080, "public": True}])
        return row

    def evidence(self, release=None):
        release = release or self.new
        artifact = {"id": 100+release.registry_revision}
        workflow = {"id": 200+release.registry_revision, "status": "completed", "conclusion": "success", "event": "push", "head_sha": SHA,
            "head_branch": "main", "repository": {"full_name": "MSG-CTF/2026_MSG_CTF"}, "path": ".github/workflows/challenge-validation.yml"}
        payload = {"challenge_slug": release.challenge_slug, "registry_revision": release.registry_revision, "source_sha": SHA,
            "scan_result": "PASS", "workload": {"containers": [{"name": c.name, "image": c.image_ref} for c in release.containers.all()]}}
        return artifact, workflow, payload

    def files(self, release=None, **overrides):
        release = release or self.new
        values = dict(challenge=self.challenge, artifact_id=1000+release.registry_revision, artifact_name="participant-files",
            registry_revision=release.registry_revision, source_ref="refs/heads/main", source_sha=SHA, present=False)
        values.update(overrides)
        return ChallengeUserFileBundle.objects.create(**values)

    def prepare(self, release=None):
        release = release or self.new
        self.files(release); record_verified(*self.evidence(release)); process_automatic_publications()
        return AutomaticPublication.objects.get(pk=release.pk).preparation

    def due(self, row):
        ImagePreparationRequest.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now()-timedelta(seconds=1))
        row.refresh_from_db()

    def step(self, row, response):
        self.due(row)
        with patch("apps.instances.image_preparations._call_runtime", return_value=response): process_preparation(row.pk)
        row.refresh_from_db()

    def activate(self, row):
        self.step(row, accepted(row))
        self.step(row, detail(row, states=("READY", "READY")))
        self.step(row, readiness(row))
        process_automatic_publications()

    def test_ci_provenance_failed_scan_and_image_mismatch_do_not_schedule(self):
        for key, value in [("conclusion", "failure"), ("event", "pull_request"), ("head_branch", "other"),
                           ("path", "unreviewed.yml"), ("head_sha", "b"*40), ("repository", {"full_name": "elsewhere/fork"})]:
            artifact, workflow, payload = self.evidence(); workflow[key] = value
            record_verified(artifact, workflow, payload)
        artifact, workflow, payload = self.evidence(); payload["scan_result"] = "FAIL"
        record_verified(artifact, workflow, payload)
        artifact, workflow, payload = self.evidence(); payload["workload"]["containers"][0]["image"] += "tampered"
        record_verified(artifact, workflow, payload)
        self.assertFalse(AutomaticPublication.objects.exists())
        with override_settings(AUTO_ACTIVATE_PUBLISHED_RELEASES=False): record_verified(*self.evidence())
        self.assertFalse(AutomaticPublication.objects.exists())

    def test_participant_files_and_environment_are_bound_to_the_same_publication(self):
        record_verified(*self.evidence()); process_automatic_publications()
        self.assertEqual(AutomaticPublication.objects.get().error_code, "WAITING_FOR_PARTICIPANT_FILES")
        bundle = self.files(source_sha="b"*40); process_automatic_publications()
        self.assertFalse(ImagePreparationRequest.objects.exists())
        bundle.source_sha = SHA; bundle.present = True; bundle.save(); process_automatic_publications()
        self.assertFalse(ImagePreparationRequest.objects.exists())
        bundle.object_key = "version-bound-file"; bundle.save(); process_automatic_publications()
        self.assertEqual(ImagePreparationRequest.objects.count(), 1)

    def test_missing_environment_or_changed_port_profile_keeps_current_release(self):
        record_verified(*self.evidence()); self.files()
        self.new.repository_env = {"missing": ["FLAG"]}; self.new.save(); process_automatic_publications()
        self.assertEqual(AutomaticPublication.objects.get().state, "HELD")
        self.config.refresh_from_db(); self.assertEqual(self.config.current_release_id, self.old.pk)

    def test_changed_public_port_is_held(self):
        self.new.containers.update(ports=[{"port": 8080, "public": False}])
        record_verified(*self.evidence()); self.files(); process_automatic_publications()
        self.assertEqual(AutomaticPublication.objects.get().error_code, "EXECUTION_PROFILE_CHANGED")

    def test_successful_preparation_is_not_enough_all_nodes_need_fresh_readiness(self):
        row = self.prepare()
        for _ in range(3): record_verified(*self.evidence()); process_automatic_publications()
        self.assertEqual(ImagePreparationRequest.objects.count(), 1)
        self.step(row, accepted(row)); self.step(row, detail(row, states=("READY", "READY")))
        self.config.refresh_from_db(); self.assertEqual(self.config.current_release_id, self.old.pk)
        data = readiness(row); data["targets"][0]["ready"] = False
        self.step(row, data)
        self.config.refresh_from_db(); self.assertEqual(self.config.current_release_id, self.old.pk)
        self.step(row, readiness(row)); process_automatic_publications()
        self.config.refresh_from_db(); self.assertEqual(self.config.current_release_id, self.new.pk)
        self.assertEqual(AutomaticPublication.objects.get().state, "ACTIVATED")
        self.assertEqual(AdminEvent.objects.filter(type="CHALLENGE_RELEASE_ACTIVATED").count(), 1)
        process_automatic_publications()
        self.assertEqual(AdminEvent.objects.filter(type="CHALLENGE_RELEASE_ACTIVATED").count(), 1)

    def test_policy_registration_lag_retries_same_request_beyond_normal_failure_limit(self):
        row = self.prepare()
        for _ in range(5):
            self.due(row)
            with patch("apps.instances.image_preparations._call_runtime", side_effect=HTTPError("https://runtime.example", 422, "pending", {}, BytesIO())):
                process_preparation(row.pk)
            row.refresh_from_db()
            self.assertEqual(row.last_error_code, "RUNTIME_POLICY_PENDING")
            self.assertEqual(row.consecutive_failures, 0)
            self.assertIsNotNone(row.next_attempt_at)
        ImagePreparationRequest.objects.filter(pk=row.pk).update(created_at=timezone.now()-timedelta(minutes=31))
        self.due(row)
        with patch("apps.instances.image_preparations._call_runtime", side_effect=HTTPError("https://runtime.example", 422, "rejected", {}, BytesIO())):
            process_preparation(row.pk)
        row.refresh_from_db(); self.assertEqual(row.activation_status, "FAILED")
        self.assertIsNone(row.next_attempt_at)

    def test_manual_rollback_or_switch_fences_pending_and_never_replays(self):
        row = self.prepare()
        self.config.refresh_from_db(); self.config.activation_generation += 1; self.config.save()
        self.activate(row)
        self.config.refresh_from_db(); self.assertEqual(self.config.current_release_id, self.old.pk)
        self.assertEqual(AutomaticPublication.objects.get().state, "SUPERSEDED")
        record_verified(*self.evidence()); process_automatic_publications()
        self.assertEqual(ImagePreparationRequest.objects.count(), 1)

    def test_newer_publication_supersedes_old_unstarted_intent(self):
        record_verified(*self.evidence())
        newer = self.make_release(3); self.files(newer); record_verified(*self.evidence(newer))
        process_automatic_publications()
        self.assertEqual(AutomaticPublication.objects.get(pk=self.new.pk).state, "SUPERSEDED")
        self.assertEqual(AutomaticPublication.objects.get(pk=newer.pk).state, "PREPARING")
        self.assertEqual(ImagePreparationRequest.objects.count(), 1)

    def test_arrival_during_previous_automatic_preparation_survives_its_activation(self):
        row = self.prepare()
        newer = self.make_release(3); record_verified(*self.evidence(newer))
        self.step(row, accepted(row)); self.step(row, detail(row, states=("READY", "READY"))); self.step(row, readiness(row))
        self.files(newer); process_automatic_publications()
        self.assertEqual(AutomaticPublication.objects.get(pk=newer.pk).state, "PREPARING")
        self.assertEqual(ImagePreparationRequest.objects.count(), 2)

    def test_periodic_worker_reconciles_publications_without_an_admin_request(self):
        self.files(); record_verified(*self.evidence())
        with patch("apps.instances.image_preparations._call_runtime", side_effect=TimeoutError):
            call_command("process_image_preparations", stdout=StringIO())
        self.assertEqual(AutomaticPublication.objects.get().state, "PREPARING")
