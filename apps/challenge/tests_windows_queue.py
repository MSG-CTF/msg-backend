import io
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.challenge.checker_dispatch import dispatch_submission
from apps.challenge.exe_submissions import ExeSubmissionError, serialize_exe_submission
from apps.challenge.models import Challenge, ExeSubmission, Solve
from apps.challenge.tests_exe_submissions import ExeFixtures
from apps.challenge import tests_windows_direct as direct


class WindowsTeamQueueTests(ExeFixtures, TestCase):
    dispatch = direct.WindowsDirectRuntimeTests.dispatch
    runtime = direct.WindowsDirectRuntimeTests.runtime
    result = direct.WindowsDirectRuntimeTests.result
    event = direct.WindowsDirectRuntimeTests.event

    def setUp(self):
        super().setUp()
        from django.conf import settings
        checker = dict(settings.EXE_CHECKERS[direct.POOL_ID])
        checker.pop("vm_template_sha256")
        checker.pop("driver_sha256")
        configured = override_settings(EXE_CHECKERS={direct.POOL_ID: checker})
        configured.enable()
        self.addCleanup(configured.disable)

    def acknowledge(self, row, **queue):
        result = {"submission_id": str(row.pk), "assignment_id": str(row.lease_id), "checker_job_id": row.checker_job_id or str(uuid.uuid4()), **queue}
        with patch("apps.challenge.checker_dispatch.checker_request", return_value=result) as request:
            dispatch_submission(row.pk)
        row.refresh_from_db()
        return request

    def test_receipt_is_queued_and_only_one_request_per_team_is_stored(self):
        row = self.accepted()
        data = serialize_exe_submission(row)
        self.assertEqual((data["status"], data["queue_policy"], data["queue_position"]), ("QUEUED", "TEAM_FIFO", None))
        ExeSubmission.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(minutes=2))
        denied = self.post_exe()
        self.assertEqual((denied.status_code, denied.data["code"]), (409, "EXECUTION_IN_PROGRESS"))
        replay = self.post_exe(key=row.idempotency_key)
        self.assertEqual((replay.status_code, replay.data["data"]["submission_id"]), (200, str(row.pk)))
        self.assertEqual(ExeSubmission.objects.count(), 1)
        self.assertEqual(len(list(self.root.rglob("payload.exe"))), 1)
        self.accepted_other_team()
        self.assertEqual(ExeSubmission.objects.count(), 2)
        self.assertFalse(Solve.objects.exists())

    def test_queue_slot_covers_the_team_across_windows_challenges(self):
        row = self.accepted()
        other = Challenge.objects.create(challenge_slug="other-windows-fixture", title="Other Windows", category="PWN", difficulty="EASY", score=100)
        ExeSubmission.objects.filter(pk=row.pk).update(challenge=other, created_at=timezone.now() - timedelta(minutes=2))
        denied = self.post_exe()
        self.assertEqual((denied.status_code, denied.data["code"]), (409, "EXECUTION_IN_PROGRESS"))

    def test_runtime_position_is_cached_and_refreshed_without_reposting(self):
        row = self.accepted()
        self.acknowledge(row, queue_state="QUEUED", queue_position=3)
        with patch("apps.challenge.checker_dispatch.checker_request") as request:
            data = self.client.get(f"{self.url}/{row.pk}").data["data"]
        request.assert_not_called()
        self.assertEqual((data["status"], data["queue_position"], data["requires_review"]), ("QUEUED", 3, False))
        self.assertIsNotNone(data["queue_observed_at"])
        row.config_snapshot["runtime_queue"]["checked_at"] = (timezone.now() - timedelta(seconds=6)).isoformat()
        row.save(update_fields=["config_snapshot"])
        response = {"submission_id": str(row.pk), "assignment_id": str(row.lease_id), "checker_job_id": row.checker_job_id, "queue_state": "QUEUED", "queue_position": 1}
        with patch("apps.challenge.checker_dispatch.checker_request", return_value=response) as request:
            data = self.client.get(f"{self.url}/{row.pk}").data["data"]
        self.assertEqual(request.call_args.args[1], "GET")
        self.assertEqual(data["queue_position"], 1)
        self.acknowledge(row, queue_state="PREPARING")
        self.assertEqual(serialize_exe_submission(row)["status"], "PREPARING")
        self.assertIsNone(serialize_exe_submission(row)["queue_position"])

    def test_stale_and_invalid_positions_are_not_presented_as_current(self):
        row = self.accepted()
        for value in [True, "3", 0, -1, 1.5, 1000001]:
            self.acknowledge(row, queue_state="QUEUED", queue_position=value)
            self.assertIsNone(serialize_exe_submission(row)["queue_position"])

    def test_detail_refresh_has_a_short_timeout_and_does_not_reload_executable(self):
        row = self.accepted()
        self.acknowledge(row, queue_state="QUEUED", queue_position=2)
        row.config_snapshot["runtime_queue"]["checked_at"] = (timezone.now() - timedelta(seconds=6)).isoformat()
        row.save(update_fields=["config_snapshot"])
        response = {"code":"SUCCESS","data":{"submission_id":str(row.pk),"assignment_id":str(row.lease_id),"checker_job_id":row.checker_job_id,"queue_state":"QUEUED","queue_position":1}}
        opener = self.http_response(json.dumps(response).encode())
        with patch("apps.challenge.checker_dispatch.build_opener",return_value=opener):
            data = self.client.get(f"{self.url}/{row.pk}").data["data"]
        self.assertEqual(opener.open.call_args.kwargs["timeout"],2)
        self.assertEqual(opener.open.call_args.args[0].method,"GET")
        self.assertEqual(data["queue_position"],1)
        self.acknowledge(row, queue_state="QUEUED", queue_position=2)
        for observed in [(timezone.now() - timedelta(seconds=16)).isoformat(), (timezone.now() + timedelta(seconds=16)).isoformat(), "bad", "2026-99-99", None, 42]:
            row.config_snapshot["runtime_queue"]["observed_at"] = observed
            self.assertIsNone(serialize_exe_submission(row)["queue_position"])

    def test_lost_refresh_keeps_confirmed_receipt_but_lost_first_post_is_unknown(self):
        row = self.accepted()
        self.acknowledge(row, queue_state="QUEUED", queue_position=2)
        with patch("apps.challenge.checker_dispatch.checker_request", side_effect=ExeSubmissionError("CHECKER_RESPONSE_UNKNOWN", "fixture", 503)):
            dispatch_submission(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.dispatch_state, "CONFIRMED")
        self.assertFalse(serialize_exe_submission(row)["requires_review"])
        other = self.accepted_other_team()
        with patch("apps.challenge.checker_dispatch.checker_request", side_effect=ExeSubmissionError("CHECKER_RESPONSE_UNKNOWN", "fixture", 503)):
            dispatch_submission(other.pk)
        other.refresh_from_db()
        self.assertEqual(serialize_exe_submission(other)["status"], "START_UNKNOWN")
        self.assertTrue(serialize_exe_submission(other)["requires_review"])

    def test_worker_advances_other_team_before_finished_team_rejoins(self):
        first = self.accepted()
        self.dispatch(first)
        self.assertEqual(self.result(first).status_code, 200)
        ExeSubmission.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(minutes=2))
        ahead = self.accepted_other_team()
        again = self.accepted()
        called = []
        def receipt(row, method):
            called.append((row.pk, method))
            return {"submission_id": str(row.pk), "assignment_id": str(row.lease_id), "checker_job_id": str(uuid.uuid4()), "queue_state": "QUEUED", "queue_position": 1}
        with patch("apps.challenge.checker_dispatch.checker_request", side_effect=receipt):
            call_command("dispatch_exe_submissions", limit=1, stdout=io.StringIO())
            call_command("dispatch_exe_submissions", limit=1, stdout=io.StringIO())
        self.assertEqual(called, [(ahead.pk, "POST"), (again.pk, "POST")])

    def test_worker_rotates_refreshes_instead_of_repolling_only_oldest_job(self):
        first, second = self.accepted(), self.accepted_other_team()
        self.acknowledge(first, queue_state="QUEUED", queue_position=1)
        self.acknowledge(second, queue_state="QUEUED", queue_position=2)
        called=[]
        def receipt(row, method):
            called.append((row.pk,method))
            return {"submission_id":str(row.pk),"assignment_id":str(row.lease_id),"checker_job_id":row.checker_job_id}
        with patch("apps.challenge.checker_dispatch.checker_request",side_effect=receipt):
            call_command("dispatch_exe_submissions",limit=1,stdout=io.StringIO())
            call_command("dispatch_exe_submissions",limit=1,stdout=io.StringIO())
        self.assertEqual(called,[(first.pk,"GET"),(second.pk,"GET")])
