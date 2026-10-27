import hashlib
import io
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.board.models import TeamChallengeAccess
from apps.challenge import tests_remote_run as remote_tests
from apps.challenge.checker_auth import mint_job_token
from apps.challenge.exe_submissions import ExeSubmissionError
from apps.challenge.models import ExeSubmission
from apps.challenge.tests_exe_submissions import ExeFixtures, harmless_pe
from apps.instances.models import ChallengeRuntimeConfig


class PrivateQaExeTests(ExeFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.challenge.is_published = False
        self.challenge.save(update_fields=["is_published"])
        ChallengeRuntimeConfig.objects.filter(challenge=self.challenge).delete()
        self.release.delete()
        self.admin = User.objects.create_user(
            login_id="qa-approver", nickname="approver", role=Role.ADMIN
        )
        self.payload = harmless_pe()
        self.source = self.root / "canary.exe"
        self.source.write_bytes(self.payload)
        self.key = uuid.uuid4()
        self.options = {
            "user_id": self.user.pk,
            "team_id": self.team.pk,
            "challenge_id": self.challenge.pk,
            "file": self.source,
            "sha256": hashlib.sha256(self.payload).hexdigest(),
            "idempotency_key": self.key,
            "approved_by": self.admin.login_id,
            "approval_reference": "approved-windows-canary-1",
            "checker_commit_sha": "a" * 40,
            "config_version": "fixture-v1",
        }

    def submit(self, **options):
        output = io.StringIO()
        call_command("submit_private_qa_exe", stdout=output, **(self.options | options))
        return json.loads(output.getvalue())

    def accepted_qa(self):
        return ExeSubmission.objects.get(pk=self.submit()["submission_id"])

    def assert_no_submission(self):
        self.assertFalse(ExeSubmission.objects.exists())
        self.assertFalse(list(self.root.rglob("payload.exe")))
        self.challenge.refresh_from_db()
        self.assertFalse(self.challenge.is_published)

    def test_approved_command_stores_one_private_submission_without_dispatch(self):
        with patch("apps.challenge.checker_dispatch.checker_request") as runtime:
            receipt = self.submit()
        runtime.assert_not_called()
        row = ExeSubmission.objects.get(pk=receipt["submission_id"])
        self.assertTrue(receipt["created"])
        self.assertEqual((row.status, row.dispatch_state), ("ASSIGNED", "PENDING"))
        self.assertIsNone(row.release_id)
        self.assertEqual(
            row.config_snapshot["private_qa"]["reference"],
            self.options["approval_reference"],
        )
        self.assertEqual(
            row.config_snapshot["private_qa"]["approved_by"], self.admin.login_id
        )
        self.assertEqual((self.root / row.payload_key).read_bytes(), self.payload)
        self.challenge.refresh_from_db()
        self.team.refresh_from_db()
        self.assertFalse(self.challenge.is_published)
        self.assertFalse(self.team.is_banned)
        self.assertEqual(TeamChallengeAccess.objects.filter(team=self.team).count(), 1)

    def test_same_key_reuses_submission_and_changed_approval_is_rejected(self):
        first = self.submit()
        second = self.submit()
        self.assertFalse(second["created"])
        self.assertEqual(first["submission_id"], second["submission_id"])
        with self.assertRaisesMessage(CommandError, "IDEMPOTENCY_KEY_CONFLICT"):
            self.submit(approval_reference="different-approval")
        self.assertEqual(ExeSubmission.objects.count(), 1)
        self.assertEqual(len(list(self.root.rglob("payload.exe"))), 1)

    def test_one_approval_cannot_create_another_submission_with_a_new_key(self):
        row = self.accepted_qa()
        ExeSubmission.objects.filter(pk=row.pk).update(
            status="COMPLETED",
            attempt_state="USED",
            finished_at=timezone.now(),
            cleanup_complete=True,
            cleanup_confirmed_at=timezone.now(),
            lease_released_at=timezone.now(),
            created_at=timezone.now() - timedelta(minutes=2),
        )
        with self.assertRaisesMessage(CommandError, "QA_APPROVAL_ALREADY_USED"):
            self.submit(idempotency_key=uuid.uuid4())
        self.assertEqual(ExeSubmission.objects.count(), 1)

    def test_public_api_still_rejects_unpublished_problem_and_qa_fields(self):
        self.assertEqual(self.post_exe().status_code, 403)
        self.assertEqual(self.post_exe(qa_approval="approved").status_code, 400)
        self.assert_no_submission()

    def test_missing_user_team_mismatch_or_non_participant_is_rejected(self):
        with self.assertRaisesMessage(CommandError, "QA_USER_NOT_FOUND"):
            self.submit(user_id=uuid.uuid4())
        with self.assertRaisesMessage(CommandError, "QA_APPROVAL_MISMATCH"):
            self.submit(team_id=uuid.uuid4())
        for attributes in (
            {"role": Role.ADMIN},
            {"is_staff": True},
            {"is_superuser": True},
        ):
            with self.subTest(attributes=attributes):
                User.objects.filter(pk=self.user.pk).update(**attributes)
                with self.assertRaisesMessage(CommandError, "QA_APPROVAL_MISMATCH"):
                    self.submit()
                User.objects.filter(pk=self.user.pk).update(
                    role=Role.PARTICIPANT, is_staff=False, is_superuser=False
                )
        self.assert_no_submission()

    def test_banned_team_and_missing_access_are_not_bypassed(self):
        self.team.is_banned = True
        self.team.save(update_fields=["is_banned"])
        with self.assertRaisesMessage(CommandError, "TEAM_BANNED"):
            self.submit()
        self.team.refresh_from_db()
        self.assertTrue(self.team.is_banned)
        self.team.is_banned = False
        self.team.save(update_fields=["is_banned"])
        TeamChallengeAccess.objects.filter(team=self.team).delete()
        with self.assertRaisesMessage(CommandError, "CHALLENGE_LOCKED"):
            self.submit()
        self.assert_no_submission()

    def test_published_or_other_problem_is_not_a_private_qa_target(self):
        self.challenge.is_published = True
        self.challenge.save(update_fields=["is_published"])
        with self.assertRaisesMessage(CommandError, "QA_REQUIRES_PRIVATE_CHALLENGE"):
            self.submit()
        self.challenge.is_published = False
        self.challenge.challenge_slug = "another-problem"
        self.challenge.save(update_fields=["is_published", "challenge_slug"])
        with self.assertRaisesMessage(CommandError, "EXE_SUBMISSION_NOT_SUPPORTED"):
            self.submit()
        self.assert_no_submission()

    def test_approval_file_hash_and_execution_version_must_match(self):
        for options in (
            {"approved_by": "unknown-approver"},
            {"approved_by": self.user.login_id},
            {"approval_reference": ""},
            {"approval_reference": "bad\nreference"},
            {"sha256": "0" * 64},
        ):
            with self.subTest(options=options):
                with self.assertRaisesMessage(CommandError, "QA_APPROVAL_MISMATCH"):
                    self.submit(**options)
        for options in (
            {"checker_commit_sha": "b" * 40},
            {"config_version": "other-version"},
        ):
            with self.subTest(options=options):
                with self.assertRaisesMessage(
                    CommandError, "QA_EXECUTION_VERSION_CHANGED"
                ):
                    self.submit(**options)
        self.assert_no_submission()

    def test_file_validation_and_runtime_preflight_remain_required(self):
        with self.assertRaisesMessage(CommandError, "QA_FILE_UNAVAILABLE"):
            self.submit(file=self.root / "missing.exe")
        self.source.write_bytes(b"not-an-executable")
        with self.assertRaisesMessage(CommandError, "INVALID_EXECUTABLE"):
            self.submit()
        self.source.write_bytes(self.payload)
        with override_settings(EXE_SUBMISSION_STORAGE_ROOT=""):
            with self.assertRaisesMessage(CommandError, "EXECUTION_NOT_READY"):
                self.submit()
        self.assert_no_submission()

    def test_global_and_problem_switches_are_not_bypassed(self):
        for flag in (
            "EXE_SUBMISSIONS_ENABLED",
            "EXE_DISPATCH_ENABLED",
            "EXE_WINDOWS_DIRECT_RUNTIME_ENABLED",
        ):
            with self.subTest(flag=flag), override_settings(**{flag: False}):
                with self.assertRaises(CommandError):
                    self.submit()
        self.remote.enabled = False
        self.remote.save(update_fields=["enabled"])
        with self.assertRaisesMessage(CommandError, "EXECUTION_NOT_READY"):
            self.submit()
        self.assert_no_submission()

    def test_normal_worker_skips_private_qa_records(self):
        row = self.accepted_qa()
        with patch(
            "apps.challenge.management.commands.dispatch_exe_submissions.dispatch_submission"
        ) as dispatch:
            call_command("dispatch_exe_submissions", stdout=io.StringIO())
        dispatch.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.dispatch_state, "PENDING")

    def test_cooldown_and_unresolved_execution_are_not_bypassed(self):
        row = self.accepted_qa()
        options = {
            "idempotency_key": uuid.uuid4(),
            "approval_reference": "approved-second-trial",
        }
        with self.assertRaisesMessage(CommandError, "EXE_RATE_LIMITED"):
            self.submit(**options)
        ExeSubmission.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(minutes=2)
        )
        with self.assertRaisesMessage(CommandError, "EXECUTION_IN_PROGRESS"):
            self.submit(**options)
        self.assertEqual(ExeSubmission.objects.count(), 1)

    def dispatch(self, row):
        response = self.http_response(
            json.dumps(
                {
                    "code": "SUCCESS",
                    "data": {
                        "submission_id": str(row.pk),
                        "assignment_id": str(row.lease_id),
                        "checker_job_id": str(uuid.uuid4()),
                    },
                }
            ).encode()
        )
        output = io.StringIO()
        with patch(
            "apps.challenge.checker_dispatch.build_opener", return_value=response
        ):
            call_command(
                "dispatch_exe_submissions", submission_id=row.pk, stdout=output
            )
        return json.loads(output.getvalue()), response

    def test_selected_dispatch_does_not_process_other_jobs_or_cleanup(self):
        row = self.accepted_qa()
        self.challenge.is_published = True
        self.challenge.save(update_fields=["is_published"])
        other = self.accepted_other_team()
        self.challenge.is_published = False
        self.challenge.save(update_fields=["is_published"])
        with patch(
            "apps.challenge.management.commands.dispatch_exe_submissions.close_exe_submission"
        ) as cleanup:
            receipt, response = self.dispatch(row)
        cleanup.assert_not_called()
        self.assertEqual(receipt["dispatch_state"], "CONFIRMED")
        self.assertEqual(
            json.loads(response.open.call_args.args[0].data)["submission_id"],
            str(row.pk),
        )
        other.refresh_from_db()
        self.assertEqual(other.dispatch_state, "PENDING")

    def test_runtime_version_drift_blocks_first_dispatch(self):
        from django.conf import settings

        row = self.accepted_qa()
        pool = str(row.worker_id)
        mapping = {pool: {**settings.EXE_CHECKERS[pool], "config_version": "changed"}}
        with override_settings(EXE_CHECKERS=mapping), patch(
            "apps.challenge.checker_dispatch.checker_request"
        ) as runtime:
            output = io.StringIO()
            call_command(
                "dispatch_exe_submissions", submission_id=row.pk, stdout=output
            )
        runtime.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue())["last_error_code"],
            "QA_EXECUTION_VERSION_CHANGED",
        )
        row.refresh_from_db()
        self.assertEqual(row.dispatch_state, "PENDING")
        self.assertTrue((self.root / row.payload_key).exists())

    def test_unknown_dispatch_reconciles_same_submission_only_by_get(self):
        row = self.accepted_qa()
        with patch(
            "apps.challenge.checker_dispatch.checker_request",
            side_effect=ExeSubmissionError("CHECKER_RESPONSE_UNKNOWN", "unknown", 503),
        ) as runtime:
            for _ in range(2):
                call_command(
                    "dispatch_exe_submissions",
                    submission_id=row.pk,
                    stdout=io.StringIO(),
                )
        self.assertEqual(
            [call.args[1] for call in runtime.call_args_list], ["POST", "GET"]
        )

    def test_selected_mode_rejects_unknown_and_legacy_ids(self):
        with self.assertRaisesMessage(CommandError, "SUBMISSION_NOT_FOUND"):
            call_command("dispatch_exe_submissions", submission_id=uuid.uuid4())
        row = self.accepted_qa()
        row.config_snapshot.pop("assignment_mode")
        row.save(update_fields=["config_snapshot"])
        with self.assertRaisesMessage(CommandError, "LEGACY_EXECUTION_REQUIRES_REVIEW"):
            call_command("dispatch_exe_submissions", submission_id=row.pk)

    def test_cli_rejects_invalid_uuid_and_conflicting_batch_scope(self):
        with self.assertRaises(CommandError):
            call_command("dispatch_exe_submissions", "--submission-id", "invalid")
        with self.assertRaises(CommandError):
            call_command(
                "dispatch_exe_submissions",
                "--submission-id",
                str(uuid.uuid4()),
                "--limit",
                "1",
            )

    def test_private_payload_and_result_remain_available_to_scoped_runtime(self):
        row = self.accepted_qa()
        self.dispatch(row)
        row.refresh_from_db()
        runtime = APIClient()
        runtime.credentials(HTTP_AUTHORIZATION="Bearer " + mint_job_token(row))
        response = runtime.get(f"/internal/v1/checker-submissions/{row.pk}/payload")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), self.payload)
        result = remote_tests.RemoteRunTests.event(self, row)
        response = runtime.post(
            f"/internal/v1/checker-submissions/{row.pk}/events", result, format="json"
        )
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.stdout, "participant output")
        self.assertIsNone(row.cleanup_complete)
        self.assertTrue((self.root / row.payload_key).exists())
        self.assertEqual(self.client.get(f"{self.url}/{row.pk}").status_code, 403)
        self.challenge.refresh_from_db()
        self.assertFalse(self.challenge.is_published)
