import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from .models import KothChallenge, KothClub, KothScorePeriod, KothSolve
from .services import ScoreFetchError, _request_scores, _validated_results, poll_challenge_period


class ScorePeriodAnchorTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="boundary-qa")
        self.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="boundary-club"), title="Boundary",
            open_group=1, inbound_internal_token_hash="a" * 64,
            score_api_url="https://score.example/internal/koth/scores",
            score_api_token_env="KOTH_QA_TOKEN",
        )
        self.period = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)

    def payload(self, period_id):
        return {"code": "SUCCESS", "data": {
            "koth_challenge_id": str(self.challenge.pk), "period_id": period_id,
            "results": [{"team_id": str(self.team.pk), "period_rank": 1, "metric_score": 123}],
            "total_count": 1,
        }}

    def fetch(self, period):
        response = SimpleNamespace(status=200, read=lambda: b'{"code":"SUCCESS","data":null}')
        transport = MagicMock()
        transport.getresponse.return_value.__enter__.return_value = response
        with patch.dict("os.environ", {"KOTH_QA_TOKEN": "owned-qa"}), patch(
            "apps.koth.services.http.client.HTTPSConnection", return_value=transport
        ):
            _request_scores(self.challenge, period)
        return parse_qs(urlsplit(transport.request.call_args.args[1]).query)

    def test_existing_start_provider_keeps_its_wire_time(self):
        self.assertEqual(self.fetch(self.period), {
            "period_id": ["2026-11-08T01:00:00Z"], "scored_at": ["2026-11-08T01:00:00Z"],
        })

    def test_end_provider_reads_the_end_of_the_same_interval(self):
        self.challenge.score_period_anchor = "END"
        self.assertEqual(self.fetch(self.period), {
            "period_id": ["2026-11-08T01:15:00Z"], "scored_at": ["2026-11-08T01:15:00Z"],
        })

    def test_end_boundary_crosses_the_utc_date(self):
        self.challenge.score_period_anchor = "END"
        value = self.fetch(datetime(2026, 11, 8, 23, 45, tzinfo=timezone.utc))
        self.assertEqual(value["period_id"], ["2026-11-09T00:00:00Z"])

    def test_end_response_is_checked_against_the_requested_provider_time(self):
        self.challenge.score_period_anchor = "END"
        with self.assertRaises(ScoreFetchError):
            _validated_results(self.challenge, self.period, self.payload("2026-11-08T01:00:00Z"))
        self.assertEqual(len(_validated_results(
            self.challenge, self.period, self.payload("2026-11-08T01:15:00Z")
        )), 1)

    def test_start_provider_still_rejects_an_end_boundary_response(self):
        with self.assertRaises(ScoreFetchError):
            _validated_results(self.challenge, self.period, self.payload("2026-11-08T01:15:00Z"))

    def test_end_award_keeps_raw_response_and_canonical_idempotency_key(self):
        self.challenge.score_period_anchor = "END"
        payload = self.payload("2026-11-08T01:15:00Z")
        with patch("apps.koth.services._request_scores", return_value=payload) as fetch:
            self.assertTrue(poll_challenge_period(self.challenge, self.period))
            self.assertFalse(poll_challenge_period(self.challenge, self.period))
        self.assertEqual(fetch.call_count, 1)
        record = KothScorePeriod.objects.get(challenge=self.challenge)
        self.assertEqual(record.period_id, self.period)
        self.assertEqual(record.response_payload, payload)
        self.assertEqual(KothSolve.objects.get(team=self.team, challenge=self.challenge).earned_score, 40)

    def test_admin_can_set_and_read_anchor_but_invalid_values_are_rejected(self):
        user = User.objects.create_user(login_id="anchor-admin", password="test-password", nickname="검증", role="ADMIN")
        client = APIClient(); client.force_authenticate(user=user)
        url = f"/api/v1/admin/koth/challenges/{self.challenge.pk}"
        response = client.patch(url, {"score_period_anchor": "END", "reason": "채점 구간 기준 정정"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["score_period_anchor"], "END")
        response = client.patch(url, {"score_period_anchor": "end", "reason": "잘못된 설정 확인"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.score_period_anchor, "END")


class ScorePeriodAnchorMigrationTests(TransactionTestCase):
    def test_existing_dependency_uses_end_and_applied_history_is_preserved(self):
        executor = MigrationExecutor(connection)
        old = [("koth", "0010_kothscoreperiod_award_snapshot")]
        new = [("koth", "0011_score_period_anchor")]
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            club = apps.get_model("koth", "KothClub")
            challenge = apps.get_model("koth", "KothChallenge")
            period = apps.get_model("koth", "KothScorePeriod")
            dependency = challenge.objects.create(
                club=club.objects.create(name="dependency-migration"), title="Dependency",
                repository_slug="koth-dependency-hell", open_group=1, inbound_internal_token_hash="d" * 64,
            )
            other = challenge.objects.create(
                club=club.objects.create(name="other-migration"), title="Other",
                repository_slug="koth-other", open_group=1, inbound_internal_token_hash="e" * 64,
            )
            recorded = period.objects.create(challenge=dependency, period_id=datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc), status="APPLIED")
            executor = MigrationExecutor(connection); executor.migrate(new)
            apps = executor.loader.project_state(new).apps
            challenge = apps.get_model("koth", "KothChallenge")
            self.assertEqual(challenge.objects.get(pk=dependency.pk).score_period_anchor, "END")
            self.assertEqual(challenge.objects.get(pk=other.pk).score_period_anchor, "START")
            self.assertEqual(apps.get_model("koth", "KothScorePeriod").objects.get(pk=recorded.pk).status, "APPLIED")
        finally:
            MigrationExecutor(connection).migrate(new)
