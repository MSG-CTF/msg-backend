import io
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, call

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase
from google.api_core.exceptions import NotFound, PreconditionFailed
from storages.backends.gcloud import GoogleCloudStorage

from apps.instances.user_file_downloads import (
    GCS_FETCH_BYTES,
    GCS_REQUEST_TIMEOUT_SECONDS,
    RESPONSE_CHUNK_BYTES,
    user_file_response,
)


class SizedReader(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.read_sizes = []

    def read(self, size=-1):
        if size < 0 or size > RESPONSE_CHUNK_BYTES:
            raise AssertionError("Download attempted an unbounded read")
        self.read_sizes.append(size)
        return super().read(size)


class UserFileStreamTests(SimpleTestCase):
    def cloud_file(self, content=b"x" * (RESPONSE_CHUNK_BYTES * 3 + 17)):
        storage = MagicMock(spec=GoogleCloudStorage)
        storage._normalize_name.side_effect = lambda key: "dev/" + key
        metadata, pinned = MagicMock(), MagicMock()
        metadata.size, metadata.generation = len(content), 123
        reader = SizedReader(content)
        pinned.open.return_value = reader
        storage.bucket.blob.side_effect = [metadata, pinned]
        bundle = SimpleNamespace(object_key="bundles/challenge/user-files.zip", size_bytes=len(content))
        return bundle, storage, metadata, pinned, reader, content

    def test_cloud_response_is_lazy_bounded_and_uses_storage_prefix(self):
        bundle, storage, metadata, pinned, reader, content = self.cloud_file()
        response = user_file_response(bundle, storage)
        pinned.open.assert_not_called()
        storage.open.assert_not_called()
        storage.exists.assert_not_called()
        self.assertEqual(storage.bucket.blob.call_args_list, [
            call("dev/bundles/challenge/user-files.zip"),
            call("dev/bundles/challenge/user-files.zip", generation=123),
        ])
        chunks = iter(response.streaming_content)
        self.assertEqual(next(chunks), content[:RESPONSE_CHUNK_BYTES])
        self.assertEqual(reader.tell(), RESPONSE_CHUNK_BYTES)
        self.assertEqual(b"".join(chunks), content[RESPONSE_CHUNK_BYTES:])
        self.assertTrue(reader.closed)
        pinned.open.assert_called_once_with(
            "rb", chunk_size=GCS_FETCH_BYTES, raw_download=True,
            timeout=GCS_REQUEST_TIMEOUT_SECONDS,
        )
        self.assertEqual(response["Content-Length"], str(len(content)))
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="user-files.zip"')

    def test_disconnect_closes_partially_read_cloud_object(self):
        bundle, storage, _, _, reader, _ = self.cloud_file()
        response = user_file_response(bundle, storage)
        next(iter(response.streaming_content))
        self.assertFalse(reader.closed)
        response.close()
        self.assertTrue(reader.closed)

    def test_head_or_unstarted_response_never_opens_a_data_reader(self):
        bundle, storage, _, pinned, _, _ = self.cloud_file()
        response = user_file_response(bundle, storage)
        response.close()
        pinned.open.assert_not_called()

    def test_missing_or_wrong_size_object_fails_before_response_headers(self):
        for failure in ["missing", "size", "generation"]:
            with self.subTest(failure=failure):
                bundle, storage, metadata, pinned, _, _ = self.cloud_file()
                if failure == "missing":
                    metadata.reload.side_effect = NotFound("missing")
                elif failure == "size":
                    metadata.size += 1
                else:
                    metadata.generation = None
                with self.assertRaises(FileNotFoundError):
                    user_file_response(bundle, storage)
                pinned.open.assert_not_called()

    def test_short_object_aborts_instead_of_finishing_a_corrupt_download(self):
        bundle, storage, metadata, _, reader, _ = self.cloud_file(b"short")
        bundle.size_bytes += 1
        # Metadata can be valid before a provider truncates a later range response.
        metadata.size = bundle.size_bytes
        response = user_file_response(bundle, storage)
        with self.assertRaises(OSError):
            b"".join(response.streaming_content)
        self.assertTrue(reader.closed)

    def test_generation_failure_is_not_retried_against_the_newest_object(self):
        bundle, storage, _, pinned, reader, _ = self.cloud_file()
        original_read = reader.read

        def read(size):
            if reader.tell():
                raise PreconditionFailed("old object generation is gone")
            return original_read(size)

        reader.read = read
        response = user_file_response(bundle, storage)
        chunks = iter(response.streaming_content)
        next(chunks)
        with self.assertRaises(PreconditionFailed):
            next(chunks)
        self.assertTrue(reader.closed)
        self.assertEqual(storage.bucket.blob.call_count, 2)
        self.assertEqual(pinned.open.call_count, 1)

    def test_local_storage_keeps_the_existing_download_contract(self):
        with tempfile.TemporaryDirectory() as root:
            storage = FileSystemStorage(location=root)
            key = storage.save("user-files.zip", ContentFile(b"local archive"))
            bundle = SimpleNamespace(object_key=key, size_bytes=13)
            response = user_file_response(bundle, storage)
            self.assertEqual(b"".join(response.streaming_content), b"local archive")
            response.close()
            with self.assertRaises(FileNotFoundError):
                user_file_response(SimpleNamespace(object_key="missing.zip"), storage)
