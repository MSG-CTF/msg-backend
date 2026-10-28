import io
import json
import os
import runpy
import secrets
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import jwt
from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework.test import APIClient

from apps.challenge.checker_auth import AUDIENCE, mint_job_token, signing_key
from apps.challenge.checker_dispatch import dispatch_submission
from apps.challenge.exe_submissions import ExeSubmissionError, close_exe_submission
from apps.challenge.models import ExeExecutionEvent, ExeSubmission, FlagSubmission, Solve
from apps.challenge.remote_run_config import remote_run_data
from apps.challenge.tests_exe_submissions import ExeFixtures, harmless_pe
from apps.challenge import tests_remote_run as remote_tests
from apps.instances.models import ChallengeRuntimeConfig
from apps.teams.models import MileageHistory


POOL_ID = "00000000-0000-0000-0000-000000000001"


class WindowsRuntimeFlagSettingsTests(SimpleTestCase):
    flags = (
        "EXE_SUBMISSIONS_ENABLED",
        "EXE_DISPATCH_ENABLED",
        "EXE_WINDOWS_DIRECT_RUNTIME_ENABLED",
    )

    def load_settings(self, **flags):
        environment = {
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
            "JWT_SECRET": secrets.token_urlsafe(48),
            **flags,
        }
        with patch.dict(os.environ, environment, clear=True), patch("dotenv.load_dotenv"):
            return runpy.run_path(
                str(Path(__file__).resolve().parents[2] / "config" / "settings.py")
            )

    def test_windows_flags_default_to_enabled(self):
        configuration = self.load_settings()
        for flag in self.flags:
            with self.subTest(flag=flag):
                self.assertIs(configuration[flag], True)

    def test_windows_flags_can_be_explicitly_disabled(self):
        for flag in self.flags:
            with self.subTest(flag=flag):
                self.assertIs(self.load_settings(**{flag: "False"})[flag], False)


class WindowsDirectRuntimeTests(ExeFixtures, TestCase):
    event = remote_tests.RemoteRunTests.event

    def setUp(self):
        super().setUp()
        checker = dict(settings.EXE_CHECKERS[POOL_ID])
        checker.pop("vm_template_sha256")
        checker.pop("driver_sha256")
        direct = override_settings(
            EXE_WINDOWS_DIRECT_RUNTIME_ENABLED=True,
            EXE_WINDOWS_RUNTIME_POOL_ID=POOL_ID,
            EXE_CHECKERS={POOL_ID: checker},
            SCHEDULER_API_TOKEN="",
        )
        direct.enable()
        self.addCleanup(direct.disable)

    def dispatch(self, row):
        opener = self.http_response(json.dumps({
            "code": "SUCCESS",
            "data": {
                "submission_id": str(row.pk),
                "assignment_id": str(row.lease_id),
                "checker_job_id": str(uuid.uuid4()),
            },
        }).encode())
        with patch("apps.challenge.checker_dispatch.build_opener", return_value=opener):
            dispatch_submission(row.pk)
        row.refresh_from_db()
        return opener.open.call_args.args[0]

    def runtime(self, row):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + mint_job_token(row))
        return client

    def remove_unrelated_release(self):
        ChallengeRuntimeConfig.objects.filter(challenge=self.challenge).delete()
        self.release.delete()

    def test_broker_pool_accepts_exe_without_backend_release(self):
        self.remove_unrelated_release()
        self.assertTrue(remote_run_data(self.challenge)["can_enable"])
        row = self.accepted()
        self.assertIsNone(row.release_id)
        self.assertEqual(row.config_snapshot["assignment_mode"], "broker_pool")

    def test_release_free_pool_request_and_token_deliver_result(self):
        self.remove_unrelated_release()
        row = self.accepted()
        outbound = self.dispatch(row)
        self.assertNotIn("release_id", json.loads(outbound.data))
        token = mint_job_token(row)
        claims = jwt.decode(token, signing_key(), algorithms=["HS256"], audience=AUDIENCE)
        self.assertNotIn("release_id", claims)
        self.assertEqual(self.result(row).status_code, 200)
        row.refresh_from_db()
        self.assertEqual((row.status, row.stdout, row.exit_code), ("COMPLETED", "participant output", 0))

    def test_existing_pool_job_keeps_its_release_binding(self):
        row = self.accepted()
        ExeSubmission.objects.filter(pk=row.pk).update(release=self.release)
        row.refresh_from_db()
        outbound = self.dispatch(row)
        self.assertEqual(json.loads(outbound.data)["release_id"], str(self.release.pk))
        claims = jwt.decode(mint_job_token(row), signing_key(), algorithms=["HS256"], audience=AUDIENCE)
        self.assertEqual(claims["release_id"], str(self.release.pk))
        self.assertEqual(self.result(row).status_code, 200)

    def refresh_token(self, row, *, token=None, body=None):
        client = APIClient()
        if token is not None:
            client.credentials(HTTP_AUTHORIZATION="Bearer " + token)
        return client.post(
            f"/internal/v1/checker-submissions/{row.pk}/token",
            {"assignment_id": str(row.lease_id), "payload_sha256": row.payload_sha256}
            if body is None else body,
            format="json",
        )

    def result(self, row, **values):
        return self.runtime(row).post(
            f"/internal/v1/checker-submissions/{row.pk}/events",
            self.event(row, **values),
            format="json",
        )

    def test_acceptance_uses_logical_pool_without_scheduler_or_vm(self):
        row = self.accepted()
        self.assertEqual(row.status, "ASSIGNED")
        self.assertEqual(str(row.worker_id), POOL_ID)
        self.assertIsNotNone(row.lease_id)
        self.assertIsNone(row.lease_expires_at)
        self.assertEqual(row.vm_readiness, "UNKNOWN")
        self.assertFalse(row.vm_id)
        self.assertEqual(row.config_snapshot["assignment_mode"], "broker_pool")
        self.assertEqual((self.root / row.payload_key).read_bytes(), harmless_pe())
        self.assertTrue(remote_run_data(self.challenge)["can_enable"])

    def test_only_selected_configured_pool_is_accepted(self):
        for pool in ("", "invalid", str(uuid.UUID(int=0)), str(uuid.uuid4()), None):
            with self.subTest(pool=pool), override_settings(EXE_WINDOWS_RUNTIME_POOL_ID=pool):
                self.assertFalse(remote_run_data(self.challenge)["can_enable"])
                self.assertEqual(self.post_exe().status_code, 409)
        self.assertFalse(ExeSubmission.objects.exists())
        self.assertFalse(list(self.root.rglob("payload.exe")))

    def test_metadata_omits_vm_file_and_secret_values(self):
        row = self.accepted()
        request = self.dispatch(row)
        body = json.loads(request.data)
        self.assertEqual(body["selection_mode"], "broker_pool")
        self.assertEqual(body["submission_id"], str(row.pk))
        self.assertEqual(body["assignment_id"], str(row.lease_id))
        self.assertEqual(body["payload_sha256"], row.payload_sha256)
        self.assertNotIn("release_id", body)
        self.assertEqual(body["timeout_seconds"], self.remote.timeout_seconds)
        for key in ("vm_id", "readiness_check_id", "vm_template_sha256", "driver_sha256", "file", "payload", "flag", "url"):
            self.assertNotIn(key, body)
        self.assertNotIn(b"local-exe-test", request.data)
        self.assertEqual(request.get_header("Idempotency-key"), str(row.pk))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + os.environ["EXE_CHECKER_FIXTURE_TOKEN"])
        self.assertEqual(row.dispatch_state, "CONFIRMED")

    def test_submission_mode_survives_later_setting_change(self):
        row = self.accepted()
        with override_settings(EXE_WINDOWS_DIRECT_RUNTIME_ENABLED=False, EXE_WINDOWS_RUNTIME_POOL_ID=""):
            request = self.dispatch(row)
        self.assertEqual(json.loads(request.data)["selection_mode"], "broker_pool")

    def test_worker_does_not_assign_direct_jobs_in_scheduler(self):
        row = self.accepted()
        with patch(
            "apps.instances.services.scheduler_request"
        ) as scheduler, patch(
            "apps.challenge.management.commands.dispatch_exe_submissions.dispatch_submission"
        ) as dispatch:
            call_command("dispatch_exe_submissions", stdout=io.StringIO())
        scheduler.assert_not_called()
        dispatch.assert_called_once_with(row.pk)

    def test_unknown_post_recovers_only_by_get_and_keeps_identifiers(self):
        row = self.accepted()
        opener = self.http_response(b"")
        opener.open.side_effect = [
            TimeoutError(),
            HTTPError("https://checker.example.invalid", 404, "missing", {}, io.BytesIO(b"{}")),
        ]
        with patch("apps.challenge.checker_dispatch.build_opener", return_value=opener):
            dispatch_submission(row.pk)
            dispatch_submission(row.pk)
        self.assertEqual([call.args[0].method for call in opener.open.call_args_list], ["POST", "GET"])
        row.refresh_from_db()
        original = (row.pk, row.lease_id, row.worker_id, row.payload_key)
        self.assertEqual(row.dispatch_state, "UNKNOWN")
        self.assertTrue((self.root / row.payload_key).exists())
        request = self.dispatch(row)
        self.assertEqual(request.method, "GET")
        self.assertEqual((row.pk, row.lease_id, row.worker_id, row.payload_key), original)
        self.assertEqual(ExeSubmission.objects.count(), 1)

    def test_payload_is_available_without_vm_readiness(self):
        row = self.accepted()
        response = self.runtime(row).get(f"/internal/v1/checker-submissions/{row.pk}/payload")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), harmless_pe())
        self.assertIn("no-store", response["Cache-Control"])

    def test_direct_job_token_cannot_fetch_flag(self):
        row = self.accepted()
        response = self.runtime(row).get(f"/internal/v1/checker-submissions/{row.pk}/secret")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b"local-exe-test", response.content)

    def test_release_free_job_token_rejects_invented_release_claim(self):
        self.remove_unrelated_release()
        row = self.accepted()
        claims = jwt.decode(mint_job_token(row), signing_key(), algorithms=["HS256"], audience=AUDIENCE)
        claims["release_id"] = str(uuid.uuid4())
        forged = jwt.encode(claims, signing_key(), algorithm="HS256")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + forged)
        response = client.get(f"/internal/v1/checker-submissions/{row.pk}/payload")
        self.assertEqual(response.status_code, 401)

    def test_other_backend_signing_key_cannot_read_or_post_result(self):
        row = self.accepted()
        self.dispatch(row)
        token = mint_job_token(row)
        claims = jwt.decode(
            token, signing_key(), algorithms=["HS256"], audience=AUDIENCE
        )
        foreign = jwt.encode(claims, secrets.token_urlsafe(48), algorithm="HS256")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + foreign)
        self.assertEqual(client.get(f"/internal/v1/checker-submissions/{row.pk}/payload").status_code, 401)
        response = client.post(f"/internal/v1/checker-submissions/{row.pk}/events", self.event(row), format="json")
        self.assertEqual(response.status_code, 401)
        row.refresh_from_db()
        self.assertIsNone(row.finished_at)

    def test_result_is_persisted_without_scoring_release_or_payload_deletion(self):
        row = self.accepted()
        self.dispatch(row)
        event = self.event(row, cleanup_complete=True)
        runtime = self.runtime(row)
        url = f"/internal/v1/checker-submissions/{row.pk}/events"
        self.assertEqual(runtime.post(url, event, format="json").status_code, 200)
        replay = runtime.post(url, event, format="json")
        self.assertTrue(replay.data["data"]["duplicate"])
        row.refresh_from_db()
        self.assertEqual((row.status, row.stdout, row.exit_code), ("COMPLETED", "participant output", 0))
        self.assertIsNone(row.vm_template_sha256)
        self.assertIsNone(row.driver_sha256)
        self.assertIsNone(row.cleanup_complete)
        self.assertIsNone(row.cleanup_confirmed_at)
        self.assertIsNone(row.lease_released_at)
        self.assertTrue((self.root / row.payload_key).exists())
        self.assertEqual(self.client.get(f"{self.url}/{row.pk}").data["data"]["status"], "CLEANUP_PENDING")
        self.assertFalse(Solve.objects.exists())
        self.assertFalse(FlagSubmission.objects.exists())
        self.assertFalse(MileageHistory.objects.exists())
        ExeSubmission.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(minutes=2))
        next_response = self.post_exe()
        self.assertEqual(next_response.status_code, 202, next_response.data)
        next_row = ExeSubmission.objects.get(pk=next_response.data["data"]["submission_id"])
        self.assertNotEqual(next_row.pk, row.pk)
        self.assertEqual(next_row.status, "ASSIGNED")
        row.refresh_from_db()
        self.assertIsNone(row.cleanup_complete)
        self.assertIsNone(row.lease_released_at)
        self.assertTrue((self.root / row.payload_key).exists())
        with self.assertRaises(ExeSubmissionError):
            close_exe_submission(row.pk)

    def test_result_from_wrong_assignment_is_rejected(self):
        row = self.accepted()
        self.dispatch(row)
        self.assertEqual(self.result(row, assignment_id=str(uuid.uuid4())).status_code, 409)
        row.refresh_from_db()
        self.assertIsNone(row.finished_at)

    def test_terminal_legacy_lease_still_blocks_next_submission(self):
        row = self.accepted()
        self.dispatch(row)
        self.assertEqual(self.result(row).status_code, 200)
        row.refresh_from_db()
        row.config_snapshot.pop("assignment_mode")
        row.created_at = timezone.now() - timedelta(minutes=2)
        row.save(update_fields=["config_snapshot", "created_at"])
        response = self.post_exe()
        self.assertEqual((response.status_code, response.data["code"]), (409, "EXECUTION_IN_PROGRESS"))

    def test_pool_result_without_finished_at_still_blocks_next_submission(self):
        row = self.accepted()
        ExeSubmission.objects.filter(pk=row.pk).update(
            status="COMPLETED", finished_at=None,
            created_at=timezone.now() - timedelta(minutes=2),
        )
        response = self.post_exe()
        self.assertEqual((response.status_code, response.data["code"]), (409, "EXECUTION_IN_PROGRESS"))

    def test_result_cannot_claim_unmeasured_vm_or_driver_digests(self):
        row = self.accepted()
        self.dispatch(row)
        response = self.result(row, vm_template_sha256="b" * 64, driver_sha256="c" * 64)
        self.assertEqual(response.status_code, 400)
        row.refresh_from_db()
        self.assertIsNone(row.finished_at)

    def test_direct_pool_ignores_static_vm_digest_settings(self):
        checker = {**settings.EXE_CHECKERS[POOL_ID], "vm_template_sha256": "b" * 64, "driver_sha256": "c" * 64}
        with override_settings(EXE_CHECKERS={POOL_ID: checker}):
            self.assertTrue(remote_run_data(self.challenge)["can_enable"])
            row = self.accepted()
            body = json.loads(self.dispatch(row).data)
            self.assertNotIn("vm_template_sha256", body)
            self.assertNotIn("driver_sha256", body)

    def test_result_can_arrive_before_post_acknowledgement(self):
        row = self.accepted()

        def reply(*_args):
            self.assertEqual(self.result(row).status_code, 200)
            raise ExeSubmissionError("CHECKER_RESPONSE_UNKNOWN", "uncertain", 503)

        with patch("apps.challenge.checker_dispatch.checker_request", side_effect=reply):
            dispatch_submission(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, "COMPLETED")
        self.assertEqual(row.dispatch_state, "CONFIRMED")
        self.assertTrue((self.root / row.payload_key).exists())

    def test_token_refresh_requires_correct_backend_service_credential(self):
        row = self.accepted()
        for token in (None, "wrong-service-token", mint_job_token(row), "non-ascii-\uac00"):
            with self.subTest(token_type="missing" if token is None else "invalid"):
                self.assertEqual(self.refresh_token(row, token=token).status_code, 401)
        response = self.refresh_token(row, token=os.environ["EXE_CHECKER_FIXTURE_TOKEN"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["assignment_id"], str(row.lease_id))
        self.assertIn("no-store", response["Cache-Control"])
        claims = jwt.decode(
            response.data["data"]["job_token"],
            signing_key(),
            algorithms=["HS256"],
            audience=AUDIENCE,
        )
        self.assertLessEqual(claims["exp"] - claims["iat"], 45 * 60)

    def test_token_refresh_rejects_other_service_and_mismatched_metadata(self):
        row = self.accepted()
        other = str(uuid.uuid4())
        mapping = dict(settings.EXE_CHECKERS)
        mapping[other] = {**mapping[POOL_ID], "service_token_env": "EXE_CHECKER_OTHER_TOKEN"}
        with override_settings(EXE_CHECKERS=mapping), patch.dict(os.environ, {"EXE_CHECKER_OTHER_TOKEN": "other-service-token-" * 3}):
            self.assertEqual(self.refresh_token(row, token=os.environ["EXE_CHECKER_OTHER_TOKEN"]).status_code, 401)
        for body in (
            {},
            {"assignment_id": str(uuid.uuid4()), "payload_sha256": row.payload_sha256},
            {"assignment_id": str(row.lease_id), "payload_sha256": "0" * 64},
            {"assignment_id": str(row.lease_id), "payload_sha256": None},
            {"assignment_id": str(row.lease_id), "payload_sha256": row.payload_sha256, "vm_id": str(uuid.uuid4())},
        ):
            with self.subTest(body_keys=list(body)):
                self.assertEqual(self.refresh_token(row, token=os.environ["EXE_CHECKER_FIXTURE_TOKEN"], body=body).status_code, 400)

    def test_expired_token_can_be_refreshed_after_runtime_wait(self):
        row = self.accepted()
        now = timezone.now()
        with patch("apps.challenge.checker_auth.timezone.now", return_value=now - timedelta(hours=2)):
            expired = mint_job_token(row)
        runtime = APIClient()
        runtime.credentials(HTTP_AUTHORIZATION="Bearer " + expired)
        url = f"/internal/v1/checker-submissions/{row.pk}/payload"
        self.assertEqual(runtime.get(url).status_code, 401)
        refreshed = self.refresh_token(row, token=os.environ["EXE_CHECKER_FIXTURE_TOKEN"])
        self.assertEqual(refreshed.status_code, 200)
        runtime.credentials(HTTP_AUTHORIZATION="Bearer " + refreshed.data["data"]["job_token"])
        response = runtime.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), harmless_pe())

    def test_token_refresh_rejects_non_ascii_sha_without_server_error(self):
        row = self.accepted()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + os.environ["EXE_CHECKER_FIXTURE_TOKEN"])
        response = client.post(
            f"/internal/v1/checker-submissions/{row.pk}/token",
            '{"assignment_id":"' + str(row.lease_id) + '","payload_sha256":"\\ud800"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_finished_result_can_refresh_for_lost_callback_ack(self):
        row = self.accepted()
        self.dispatch(row)
        event = self.event(row)
        runtime = self.runtime(row)
        url = f"/internal/v1/checker-submissions/{row.pk}/events"
        self.assertEqual(runtime.post(url, event, format="json").status_code, 200)
        refreshed = self.refresh_token(row, token=os.environ["EXE_CHECKER_FIXTURE_TOKEN"])
        self.assertEqual(refreshed.status_code, 200)
        runtime.credentials(HTTP_AUTHORIZATION="Bearer " + refreshed.data["data"]["job_token"])
        replay = runtime.post(url, event, format="json")
        self.assertTrue(replay.data["data"]["duplicate"])
        self.assertEqual(runtime.get(f"/internal/v1/checker-submissions/{row.pk}/payload").status_code, 404)

    def test_no_refresh_after_cleanup_or_local_assignment_close(self):
        row = self.accepted()
        for values in ({"cleanup_complete": True}, {"lease_released_at": timezone.now()}):
            with self.subTest(fields=list(values)):
                ExeSubmission.objects.filter(pk=row.pk).update(
                    **{"cleanup_complete": None, "lease_released_at": None, **values}
                )
                response = self.refresh_token(row, token=os.environ["EXE_CHECKER_FIXTURE_TOKEN"])
                self.assertEqual(response.status_code, 404)

    def test_confirmed_cleanup_closes_local_assignment_without_scheduler(self):
        row = self.accepted()
        self.dispatch(row)
        self.assertEqual(self.result(row).status_code, 200)
        runtime = self.runtime(row)
        response = runtime.post(
            f"/internal/v1/checker-submissions/{row.pk}/events",
            self.event(row, "CLEANUP"), format="json",
        )
        self.assertEqual(response.status_code, 200)
        with patch("apps.instances.services.scheduler_request") as scheduler:
            close_exe_submission(row.pk)
            close_exe_submission(row.pk)
        scheduler.assert_not_called()
        row.refresh_from_db()
        self.assertIsNotNone(row.lease_released_at)
        self.assertEqual(row.payload_key, "")
        self.assertFalse(list(self.root.rglob("payload.exe")))

    def test_service_receipt_confirms_cleanup_after_local_close(self):
        row = self.accepted()
        self.dispatch(row)
        self.assertEqual(self.result(row).status_code, 200)
        cleanup = self.event(row, "CLEANUP")
        url = f"/internal/v1/checker-submissions/{row.pk}/events/{cleanup['event_id']}/receipt"
        service = APIClient()
        service.credentials(HTTP_AUTHORIZATION="Bearer " + os.environ["EXE_CHECKER_FIXTURE_TOKEN"])
        missing = service.post(url, cleanup, format="json")
        self.assertEqual((missing.status_code, missing.data["data"]["accepted"]), (200, False))
        self.assertEqual(self.runtime(row).post(
            f"/internal/v1/checker-submissions/{row.pk}/events", cleanup, format="json"
        ).status_code, 200)
        close_exe_submission(row.pk)
        rotated_key = secrets.token_urlsafe(48)
        with override_settings(EXE_CHECKER_SIGNING_KEY=rotated_key):
            confirmed = service.post(url, cleanup, format="json")
            self.assertEqual((confirmed.status_code, confirmed.data["data"]["accepted"]), (200, True))
            changed = {**cleanup, "cleanup_complete": False}
            self.assertEqual(service.post(url, changed, format="json").status_code, 409)
        legacy_fingerprint = salted_hmac(
            "exe-event-v1", json.dumps(cleanup, sort_keys=True, separators=(",", ":")),
            secret=signing_key(), algorithm="sha256",
        ).hexdigest()
        ExeExecutionEvent.objects.filter(pk=cleanup["event_id"]).update(fingerprint=legacy_fingerprint)
        self.assertEqual(service.post(url, cleanup, format="json").status_code, 200)
        wrong = APIClient()
        wrong.credentials(HTTP_AUTHORIZATION="Bearer wrong-service-token")
        self.assertEqual(wrong.post(url, cleanup, format="json").status_code, 401)
        self.assertEqual(self.runtime(row).post(url, cleanup, format="json").status_code, 401)

    def test_disabled_direct_mode_blocks_acceptance_without_scheduler_fallback(self):
        with override_settings(EXE_WINDOWS_DIRECT_RUNTIME_ENABLED=False, SCHEDULER_API_TOKEN="configured-but-unused"), patch(
            "apps.instances.services.scheduler_request"
        ) as scheduler:
            self.assertFalse(remote_run_data(self.challenge)["can_enable"])
            self.assertEqual(self.post_exe().status_code, 409)
            call_command("dispatch_exe_submissions", stdout=io.StringIO())
        scheduler.assert_not_called()
        self.assertFalse(ExeSubmission.objects.exists())

    def test_legacy_records_are_not_dispatched_reassigned_or_deleted(self):
        row = self.accepted()
        row.config_snapshot.pop("assignment_mode")
        for status in ("QUEUED", "ASSIGNED", "COMPLETED"):
            row.status = status
            row.save()
            before = ExeSubmission.objects.values().get(pk=row.pk)
            with patch("apps.instances.services.scheduler_request") as scheduler, patch(
                "apps.challenge.checker_dispatch.checker_request"
            ) as checker:
                call_command("dispatch_exe_submissions", stdout=io.StringIO())
                dispatch_submission(row.pk)
                with self.assertRaises(ExeSubmissionError) as error:
                    close_exe_submission(row.pk)
            self.assertEqual(error.exception.code, "LEGACY_EXECUTION_REQUIRES_REVIEW")
            scheduler.assert_not_called()
            checker.assert_not_called()
            self.assertEqual(ExeSubmission.objects.values().get(pk=row.pk), before)
            self.assertTrue((self.root / row.payload_key).exists())
            self.assertTrue(self.client.get(f"{self.url}/{row.pk}").data["data"]["requires_review"])

    def test_legacy_submission_cannot_use_service_token_refresh(self):
        row = self.accepted()
        row.config_snapshot.pop("assignment_mode")
        row.save(update_fields=["config_snapshot"])
        self.assertEqual(self.refresh_token(row, token=os.environ["EXE_CHECKER_FIXTURE_TOKEN"]).status_code, 404)
