import base64
import io
import json
import os
import zipfile
from unittest.mock import patch

import yaml
from django.test import SimpleTestCase

from apps.instances import poller, published_challenges as publications
from apps.instances.releases import ReleaseValidationError


def bundle(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


class PublicationCompatibilityTests(SimpleTestCase):
    def metadata(self, category, *, flag="test-grading-value"):
        raw = yaml.safe_dump({"name": "Evidence", "category": category,
                              "description": "Author description", "flag": flag}).encode()
        response = {"encoding": "base64", "size": len(raw),
                    "content": base64.b64encode(raw).decode()}
        with patch.object(publications, "_json", return_value=response):
            return publications._metadata("forensic-evidence", "a" * 40)

    def test_published_forensics_category_maps_to_backend_category(self):
        self.assertEqual(self.metadata("forensics")["category"], "FORENSIC")

    def test_canonical_category_is_preserved(self):
        self.assertEqual(self.metadata("forensic")["category"], "FORENSIC")

    def test_unknown_category_is_still_rejected(self):
        with self.assertRaises(ReleaseValidationError):
            self.metadata("unknown-category")

    def test_flag_limit_counts_characters_and_preserves_whitespace(self):
        for flag in ("x", "x" * 512, "\U0001f680" * 512, " " + "x" * 510 + " "):
            with self.subTest(length=len(flag), utf8_bytes=len(flag.encode("utf-8"))):
                self.assertEqual(self.metadata("web", flag=flag)["flag"], flag)

    def test_flags_above_submission_limit_are_rejected(self):
        for flag in ("x" * 513, "\U0001f680" * 513):
            with self.subTest(utf8_bytes=len(flag.encode("utf-8"))):
                with self.assertRaises(ReleaseValidationError):
                    self.metadata("web", flag=flag)

    def test_invalid_flag_types_and_control_characters_remain_rejected(self):
        for flag in (None, 123, True, [], {}, "", "a\x00b", "a\rb", "a\nb"):
            with self.subTest(flag_type=type(flag).__name__):
                with self.assertRaises(ReleaseValidationError):
                    self.metadata("web", flag=flag)

    def download(self, raw):
        # Exercise the real bounded HTTP reader; only the network response is replaced.
        with patch("apps.instances.poller.urlopen", return_value=io.BytesIO(raw)):
            return poller.download_bundle({"id": 1})

    def test_publication_with_large_sbom_reads_only_release_manifest(self):
        payload = {"schema_version": "2.0", "challenge_slug": "pwn-price-tag"}
        raw = bundle({"artifact-v2.json": json.dumps(payload),
                      "sbom.json": os.urandom(3 * 1024 * 1024)})
        self.assertGreater(len(raw), 2 * 1024 * 1024)
        self.assertEqual(self.download(raw), payload)

    def test_publication_download_remains_bounded(self):
        with self.assertRaisesMessage(ReleaseValidationError, "크기가 허용 범위를 초과"):
            self.download(b"x" * (16 * 1024 * 1024 + 1))

    def test_compressed_oversized_manifest_is_rejected(self):
        raw = bundle({"artifact-v2.json": json.dumps({"padding": "x" * (1024 * 1024)})})
        with self.assertRaisesMessage(ReleaseValidationError, "실행 발행 파일"):
            self.download(raw)

    def test_ambiguous_release_manifests_are_rejected(self):
        raw = bundle({"artifact-v2.json": "{}", "nested/artifact-v2.json": "{}"})
        with self.assertRaisesMessage(ReleaseValidationError, "실행 발행 파일"):
            self.download(raw)
