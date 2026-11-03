import hashlib
import json
import tempfile
from unittest.mock import patch

from django.test import override_settings
from google.api_core.exceptions import Forbidden
from rest_framework.test import APIClient

from apps.adminpanel.challenge_deployment import deployment_summaries
from apps.adminpanel.models import AdminEvent
from apps.challenge.models import Challenge
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig, ChallengeUserFileSelection
from apps.instances.tests_releases import ReleaseTestBase
from apps.instances.tests_user_files import user_archive, user_artifact, user_manifest
from apps.instances.user_files import current_user_file_bundle, register_user_files_bundle


class AdminChallengeFilesTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.auth("root")
        self.challenge.challenge_slug = "web-basic"
        self.challenge.save(update_fields=["challenge_slug"])
        self.url = f"/api/v1/admin/challenges/{self.challenge.pk}/files"
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = override_settings(MEDIA_ROOT=self.temporary.name, STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        })
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        self.archive = user_archive({"README.txt": b"participant attachment"})

    def bundle(self, revision=1, present=True):
        status, bundle = register_user_files_bundle(
            user_manifest(self.archive if present else None, revision=revision),
            self.archive if present else None, user_artifact(artifact_id=revision),
        )
        self.assertEqual(status, "registered")
        return bundle

    def select(self, revision):
        release = self.create_legacy_release()
        release.registry_revision = revision
        release.save(update_fields=["registry_revision"])
        return ChallengeRuntimeConfig.objects.create(challenge=self.challenge, current_release=release)

    def test_static_file_metadata_and_admin_without_team_can_download(self):
        bundle = self.bundle()
        response = self.client.get(self.url)
        self.assertEqual(response.data["data"]["status"], "available")
        self.assertEqual(response.data["data"]["current_file"]["file_id"], str(bundle.pk))
        self.assertEqual(response.data["data"]["current_file"]["file_count"], 1)
        self.assertEqual(response.data["data"]["storage"]["provider"], "local")
        self.assertNotIn("object_key", response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])
        downloaded = self.client.get(f"{self.url}/{bundle.pk}/download")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded["Content-Type"], "application/zip")
        self.assertIn("attachment", downloaded["Content-Disposition"])
        self.assertIn("no-store", downloaded["Cache-Control"])
        self.assertEqual(hashlib.sha256(b"".join(downloaded.streaming_content)).hexdigest(), bundle.sha256)
        self.assertTrue(downloaded.closed)

    def test_current_release_and_later_history_are_distinct(self):
        old, latest = self.bundle(1), self.bundle(2)
        self.select(1)
        result = self.client.get(self.url).data["data"]
        self.assertEqual(result["registry_revision"], 1)
        self.assertEqual(result["release_version"], 1)
        self.assertEqual(result["current_file"]["file_id"], str(old.pk))
        self.assertEqual([file["file_id"] for file in result["history"]], [str(latest.pk)])
        # 운영자는 아직 사용하지 않는 파일도 검수할 수 있다
        downloaded = self.client.get(f"{self.url}/{latest.pk}/download")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(hashlib.sha256(b"".join(downloaded.streaming_content)).hexdigest(), latest.sha256)
        self.assertTrue(downloaded.closed)

    def test_no_attachment_is_distinct_from_pending_collection_or_no_selected_release(self):
        config = self.select(1)
        self.assertEqual(self.client.get(self.url).data["data"]["status"], "pending")
        self.bundle(1, present=False)
        self.assertEqual(self.client.get(self.url).data["data"]["status"], "none")
        config.current_release = None
        config.save(update_fields=["current_release"])
        result = self.client.get(self.url).data["data"]
        self.assertEqual(result["status"], "no_release")
        self.assertIsNone(result["current_file"])

    def choose(self, body, key="static-file-choice"):
        return self.client.post(f"{self.url}/selection", body, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_static_file_can_be_pinned_then_returned_to_latest(self):
        old, newer = self.bundle(1), self.bundle(2)
        result = self.client.get(self.url).data["data"]
        self.assertEqual((result["mode"], result["current_file"]["file_id"]), ("LATEST", str(newer.pk)))
        self.assertEqual(result["latest_file"]["file_id"], str(newer.pk))
        self.assertIsNone(result["selected_by"])

        pinned = self.choose({"mode": "SELECTED", "file_id": str(old.pk), "reason": "발행 2 ZIP 누락"})
        self.assertEqual(pinned.status_code, 200)
        self.assertEqual(pinned.data["data"]["current_file"]["file_id"], str(old.pk))
        # 고정한 뒤 들어온 새 발행본은 참가자에게 바로 나가지 않는다
        newest = self.bundle(3)
        result = self.client.get(self.url).data["data"]
        self.assertEqual((result["mode"], result["current_file"]["file_id"]), ("SELECTED", str(old.pk)))
        self.assertEqual(result["latest_file"]["file_id"], str(newest.pk))
        self.assertEqual(result["selected_by"], "root")
        self.assertEqual({file["file_id"] for file in result["history"]}, {str(newer.pk), str(newest.pk)})
        self.assertEqual(current_user_file_bundle(self.challenge), old)

        latest = self.choose({"mode": "LATEST"}, key="static-file-latest")
        self.assertEqual(latest.data["data"]["current_file"]["file_id"], str(newest.pk))
        self.assertEqual(current_user_file_bundle(self.challenge), newest)
        self.assertFalse(ChallengeUserFileSelection.objects.exists())
        events = list(AdminEvent.objects.filter(challenge=self.challenge).order_by("created_at"))
        self.assertEqual([event.severity for event in events], ["WARNING", "INFO"])
        self.assertEqual(events[0].message, "참가자 파일 고정: 최신 자동 발행 #2 → 발행 #1 고정: 발행 2 ZIP 누락")
        self.assertEqual(events[1].message, "참가자 파일 최신 자동: 발행 #1 고정 → 최신 자동 발행 #3")
        self.assertEqual({event.actor for event in events}, {"root"})

    def test_static_file_choice_replays_by_key_and_logs_only_changes(self):
        old, _ = self.bundle(1), self.bundle(2)
        body = {"mode": "SELECTED", "file_id": str(old.pk)}
        first = self.choose(body)
        self.assertEqual(self.choose(body).data, first.data)
        self.assertEqual(self.choose({"mode": "LATEST"}).status_code, 409)
        self.assertEqual(self.choose(body, key="same-file-again").status_code, 200)
        self.assertEqual(self.choose({"mode": "LATEST"}, key="latest-1").status_code, 200)
        self.assertEqual(self.choose({"mode": "LATEST"}, key="latest-2").status_code, 200)
        self.assertEqual(AdminEvent.objects.filter(challenge=self.challenge).count(), 2)

    def test_static_file_choice_rejects_invalid_and_runtime_requests(self):
        bundle, hidden = self.bundle(1), self.bundle(2, present=False)
        for index, (body, status) in enumerate([
            ({"mode": "SELECTED"}, 400),
            ({"mode": "SELECTED", "file_id": "not-a-uuid"}, 400),
            ({"mode": "LATEST", "file_id": str(bundle.pk)}, 400),
            ({"mode": "NONE"}, 400),
            ({"mode": "LATEST", "note": "x"}, 400),
            ({"mode": "LATEST", "reason": " "}, 400),
            ({"mode": "LATEST", "reason": "가" * 501}, 400),
            ({"mode": "SELECTED", "file_id": str(hidden.pk)}, 404),
        ]):
            self.assertEqual(self.choose(body, key=f"invalid-{index}").status_code, status, body)
        other = Challenge.objects.create(title="Other", category="WEB", difficulty="EASY", score=100, flag_hash="unused")
        foreign = self.client.post(f"/api/v1/admin/challenges/{other.pk}/files/selection",
                                   {"mode": "SELECTED", "file_id": str(bundle.pk)}, format="json", HTTP_IDEMPOTENCY_KEY="foreign")
        self.assertEqual(foreign.data["code"], "FILE_NOT_FOUND")
        self.assertEqual(self.client.post(f"{self.url}/selection", {"mode": "LATEST"}, format="json").status_code, 400)
        self.select(1)
        runtime = self.choose({"mode": "SELECTED", "file_id": str(bundle.pk)}, key="runtime")
        self.assertEqual((runtime.status_code, runtime.data["code"]), (409, "RUNTIME_CHALLENGE_FILES"))
        self.auth("player")
        self.assertEqual(self.choose({"mode": "LATEST"}, key="player").status_code, 403)
        self.assertEqual(APIClient().post(f"{self.url}/selection", {"mode": "LATEST"}, format="json").status_code, 401)
        self.assertFalse(ChallengeUserFileSelection.objects.exists())
        self.assertFalse(AdminEvent.objects.filter(challenge=self.challenge).exists())

    def test_other_challenge_file_is_not_returned(self):
        bundle = self.bundle()
        other = Challenge.objects.create(title="Other", category="WEB", difficulty="EASY", score=100, flag_hash="unused")
        response = self.client.get(f"/api/v1/admin/challenges/{other.pk}/files/{bundle.pk}/download")
        self.assertEqual(response.status_code, 404)

    def test_read_permissions_and_storage_errors(self):
        bundle = self.bundle()
        url = f"{self.url}/{bundle.pk}/download"
        with patch("apps.adminpanel.challenge_files.default_storage.exists", side_effect=Forbidden("private credential detail")):
            failure = self.client.get(url)
        self.assertEqual(failure.status_code, 503)
        self.assertNotIn("credential", json.dumps(failure.data))
        with patch("apps.adminpanel.challenge_files.default_storage.exists", return_value=False):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.auth("player")
        for route in (self.url, url):
            self.assertEqual(self.client.get(route).status_code, 403)
            self.assertEqual(APIClient().get(route).status_code, 401)

    def test_gcs_configuration_is_reported_without_network_request(self):
        self.bundle()
        with override_settings(STORAGES={"default": {"BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
                                                     "OPTIONS": {"bucket_name": "test-bucket", "location": "dev"}}}):
            with patch("apps.adminpanel.challenge_files.default_storage.exists") as exists:
                response = self.client.get(self.url)
        exists.assert_not_called()
        self.assertEqual(response.data["data"]["storage"], {"provider": "gcs", "bucket": "test-bucket"})


class AdminDeploymentSummaryTests(ReleaseTestBase):
    def test_current_version_takes_precedence_and_values_stay_out_of_summary(self):
        self.auth("root")
        current = self.create_legacy_release()
        container = current.containers.get()
        container.env = {"GREETING": "private-setting-value"}
        container.secret_env = {"FLAG": "private-secret-reference"}
        container.save(update_fields=["env", "secret_env"])
        ChallengeRuntimeConfig.objects.create(challenge=self.challenge, current_release=current)
        ChallengeRelease.objects.create(challenge=self.challenge, version=2, registry_revision=2,
                                        cpu_millicores=999, memory_mib=999, ephemeral_storage_mib=999,
                                        isolation_profile="WEB")
        response = self.client.get("/api/v1/admin/challenges")
        summary = response.data["data"]["challenges"][0]["deployment"]
        self.assertEqual(summary["version"], 1)
        self.assertTrue(summary["is_current"])
        self.assertEqual(summary["resource_profile"]["cpu_millicores"], 500)
        self.assertEqual(summary["containers"][0]["env_count"], 1)
        self.assertEqual(summary["containers"][0]["secret_count"], 1)
        self.assertNotIn("private-", json.dumps(response.data))
        self.assertIn("no-store", response["Cache-Control"])

    def test_draft_and_empty_summary_and_query_count(self):
        self.create_legacy_release()
        other = Challenge.objects.create(title="Other", category="MISC", difficulty="EASY", score=100, flag_hash="unused")
        with self.assertNumQueries(3):
            summaries = deployment_summaries([self.challenge, other])
        self.assertFalse(summaries[self.challenge.pk]["is_current"])
        self.assertIsNone(summaries[other.pk])
