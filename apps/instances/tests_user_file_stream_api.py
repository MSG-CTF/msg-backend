import io
import uuid
from unittest.mock import MagicMock, patch

from django.test import override_settings
from rest_framework.test import APIClient
from storages.backends.gcloud import GoogleCloudStorage

from apps.accounts.models import Team, User
from apps.board.models import Cell, TeamChallengeAccess
from apps.instances.tests_user_files import (
    UserFilesTestBase, user_archive, user_artifact, user_manifest,
)
from apps.instances.user_files import register_user_files_bundle


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}})
class GCSUserFileApiTests(UserFilesTestBase):
    def setUp(self):
        super().setUp()
        self.team = Team.objects.create(team_name="stream-team")
        self.user = User.objects.create_user(
            login_id="stream-user", password="test-only", nickname="stream-user", team=self.team
        )
        cell = Cell.objects.create(
            cell_index=1, type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY, name="stream-cell",
        )
        self.access = TeamChallengeAccess.objects.create(
            team=self.team, challenge=self.challenge, source_cell=cell,
        )
        self.content = user_archive()
        _, self.bundle = register_user_files_bundle(
            user_manifest(self.content), self.content, user_artifact()
        )
        self.path = f"/api/v1/challenges/{self.challenge.pk}/files/{self.bundle.pk}/download"
        self.client = APIClient()

    def cloud_storage(self):
        storage = MagicMock(spec=GoogleCloudStorage)
        storage._normalize_name.side_effect = lambda key: "dev/" + key
        metadata, pinned = MagicMock(), MagicMock()
        metadata.size, metadata.generation = len(self.content), 123
        pinned.open.return_value = io.BytesIO(self.content)
        storage.bucket.blob.side_effect = [metadata, pinned]
        return storage, pinned

    def test_download_keeps_private_headers_and_archive_bytes(self):
        self.client.force_authenticate(self.user)
        storage, pinned = self.cloud_storage()
        with patch("apps.challenge.views.default_storage", storage):
            response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["Content-Length"], str(len(self.content)))
        self.assertEqual(b"".join(response.streaming_content), self.content)
        storage.open.assert_not_called()

    def test_unauthenticated_and_locked_requests_never_touch_gcs(self):
        storage, _ = self.cloud_storage()
        with patch("apps.challenge.views.default_storage", storage):
            self.assertEqual(self.client.get(self.path).status_code, 401)
            self.client.force_authenticate(self.user)
            self.access.delete()
            self.assertEqual(self.client.get(self.path).status_code, 403)
        storage.bucket.blob.assert_not_called()

    def test_unknown_file_id_does_not_open_any_cloud_object(self):
        self.client.force_authenticate(self.user)
        storage, _ = self.cloud_storage()
        with patch("apps.challenge.views.default_storage", storage):
            response = self.client.get(self.path.replace(str(self.bundle.pk), str(uuid.uuid4())))
        self.assertEqual(response.status_code, 404)
        storage.bucket.blob.assert_not_called()

    def test_head_has_size_but_does_not_start_a_body_download(self):
        self.client.force_authenticate(self.user)
        storage, pinned = self.cloud_storage()
        with patch("apps.challenge.views.default_storage", storage):
            response = self.client.head(self.path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Length"], str(len(self.content)))
        self.assertEqual(b"".join(response.streaming_content), b"")
        pinned.open.assert_not_called()
