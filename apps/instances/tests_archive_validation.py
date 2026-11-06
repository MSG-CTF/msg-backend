import io
import json
import struct
import zipfile
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.instances import poller, user_files
from apps.instances.models import ChallengeUserFileBundle
from apps.instances.releases import ReleaseValidationError
from apps.instances.tests_user_files import (
    UserFilesTestBase,
    user_artifact,
    user_manifest,
)


def archive_bytes(entries, compression=zipfile.ZIP_STORED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return stream.getvalue()


def damaged_payload(raw, *, deflate=False):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entry = archive.infolist()[0]
    value = bytearray(raw)
    name_size, extra_size = struct.unpack_from("<HH", value, entry.header_offset + 26)
    offset = entry.header_offset + 30 + name_size + extra_size
    value[offset] = 7 if deflate else value[offset] ^ 1
    return bytes(value)


class ParticipantArchiveValidationTests(SimpleTestCase):
    def validate(self, raw):
        manifest = user_files.validate_user_files_manifest(user_manifest(raw))
        user_files.validate_user_files_archive(raw, manifest)

    def test_valid_supported_compressions_and_empty_directories(self):
        for compression in (
            zipfile.ZIP_STORED,
            zipfile.ZIP_DEFLATED,
            zipfile.ZIP_BZIP2,
            zipfile.ZIP_LZMA,
        ):
            with self.subTest(compression=compression):
                self.validate(
                    archive_bytes(
                        [
                            ("data/", b""),
                            ("data/readme.txt", b"participant files"),
                        ],
                        compression,
                    )
                )

    def test_corrupt_member_is_rejected_even_with_matching_archive_checksum(self):
        raw = damaged_payload(archive_bytes([("readme.txt", b"participant files")]))
        with self.assertRaises(ReleaseValidationError):
            self.validate(raw)

    def test_invalid_deflate_stream_is_a_validation_error(self):
        raw = damaged_payload(
            archive_bytes(
                [
                    ("readme.txt", b"participant files"),
                ],
                zipfile.ZIP_DEFLATED,
            ),
            deflate=True,
        )
        with self.assertRaises(ReleaseValidationError):
            self.validate(raw)

    def test_encrypted_and_unsupported_members_are_validation_errors(self):
        original = archive_bytes([("readme.txt", b"participant files")])
        for encrypted in (True, False):
            with self.subTest(encrypted=encrypted):
                raw = bytearray(original)
                central = raw.index(b"PK\x01\x02")
                local_offset, central_offset = (6, 8) if encrypted else (8, 10)
                value = 1 if encrypted else 99
                struct.pack_into("<H", raw, local_offset, value)
                struct.pack_into("<H", raw, central + central_offset, value)
                with self.assertRaises(ReleaseValidationError):
                    self.validate(bytes(raw))

    def test_directory_payload_cannot_bypass_expansion_limit(self):
        raw = archive_bytes([("data/", b"not a directory"), ("readme.txt", b"ok")])
        with patch.object(zipfile.ZipFile, "testzip") as crc:
            with self.assertRaises(ReleaseValidationError):
                self.validate(raw)
        crc.assert_not_called()

    def test_size_count_and_manifest_limits_are_checked_before_crc_reads(self):
        raw = archive_bytes([("one.txt", b"one"), ("two.txt", b"two")])
        cases = (
            ("MAX_UNCOMPRESSED_SIZE", 5),
            ("MAX_FILE_COUNT", 1),
        )
        for setting, limit in cases:
            with self.subTest(setting=setting):
                with patch.object(user_files, setting, limit), patch.object(
                    zipfile.ZipFile, "testzip"
                ) as crc:
                    with self.assertRaises(ReleaseValidationError):
                        self.validate(raw)
                crc.assert_not_called()
        validated = user_files.validate_user_files_manifest(user_manifest(raw))
        for field in ("file_count", "uncompressed_size_bytes"):
            with self.subTest(field=field):
                with patch.object(zipfile.ZipFile, "testzip") as crc:
                    with self.assertRaises(ReleaseValidationError):
                        user_files.validate_user_files_archive(
                            raw, {**validated, field: 0}
                        )
                crc.assert_not_called()

    def test_crc_reads_are_bounded_instead_of_loading_whole_members(self):
        raw = archive_bytes(
            [("data.txt", b"x" * (3 * 1024 * 1024))], zipfile.ZIP_DEFLATED
        )
        read = zipfile.ZipExtFile.read
        sizes = []

        def bounded_read(member, size=-1):
            sizes.append(size)
            return read(member, size)

        with patch.object(zipfile.ZipExtFile, "read", bounded_read):
            self.validate(raw)
        self.assertTrue(sizes)
        self.assertTrue(all(0 < size <= 1024 * 1024 for size in sizes))


class CorruptArchiveRegistrationTests(UserFilesTestBase):
    def test_corrupt_archive_never_reaches_storage_or_database(self):
        raw = damaged_payload(archive_bytes([("readme.txt", b"participant files")]))
        with patch("apps.instances.user_files.default_storage") as storage:
            status, _ = user_files.register_user_files_bundle(
                user_manifest(raw), raw, user_artifact()
            )
        self.assertEqual(status, "invalid")
        storage.save.assert_not_called()
        self.assertFalse(ChallengeUserFileBundle.objects.exists())


class PublishBundleValidationTests(SimpleTestCase):
    def download(self, raw):
        with patch("apps.instances.poller.urlopen", return_value=io.BytesIO(raw)):
            return poller.download_bundle({"id": 1}, token="local-test-token")

    def test_nested_manifest_and_unread_scan_reports_still_work(self):
        payload = {"challenge_slug": "web-basic"}
        raw = archive_bytes(
            [
                ("release/artifact-v2.json", json.dumps(payload)),
                ("sbom.json", b"large report"),
            ]
        )
        self.assertEqual(self.download(raw), payload)

    def test_manifest_read_is_bounded_not_just_its_declared_size(self):
        raw = archive_bytes([("artifact-v2.json", "{}")], zipfile.ZIP_DEFLATED)
        read = zipfile.ZipExtFile.read
        sizes = []

        def bounded_read(member, size=-1):
            sizes.append(size)
            return read(member, size)

        with patch.object(zipfile.ZipExtFile, "read", bounded_read):
            self.assertEqual(self.download(raw), {})
        self.assertTrue(sizes)
        self.assertTrue(all(0 < size <= 1024 * 1024 + 1 for size in sizes))

    def test_missing_or_duplicate_manifests_are_rejected(self):
        for entries in (
            [("other.json", "{}")],
            [("artifact-v2.json", "{}"), ("nested/artifact-v2.json", "{}")],
        ):
            with self.subTest(names=[name for name, _ in entries]):
                with self.assertRaises(ReleaseValidationError):
                    self.download(archive_bytes(entries))

    def test_exact_manifest_and_download_limits_are_inclusive(self):
        body = b'{"padding":"' + b"x" * (1024 * 1024 - len(b'{"padding":""}')) + b'"}'
        raw = archive_bytes([("artifact-v2.json", body)], zipfile.ZIP_DEFLATED)
        with patch.object(poller, "MAX_PUBLICATION_BUNDLE_BYTES", len(raw)):
            self.assertEqual(
                len(self.download(raw)["padding"]), len(body) - len(b'{"padding":""}')
            )
        with patch.object(poller, "MAX_PUBLICATION_BUNDLE_BYTES", len(raw) - 1):
            with self.assertRaises(ReleaseValidationError):
                self.download(raw)

    def test_invalid_json_root_and_corrupt_zip_are_validation_errors(self):
        for body in ("[]", "null", "123", "invalid json"):
            with self.subTest(body=body):
                with self.assertRaises(ReleaseValidationError):
                    self.download(archive_bytes([("artifact-v2.json", body)]))
        for raw in (
            b"not a ZIP",
            damaged_payload(
                archive_bytes([("artifact-v2.json", "{}")], zipfile.ZIP_DEFLATED),
                deflate=True,
            ),
        ):
            with self.assertRaises(ReleaseValidationError):
                self.download(raw)
