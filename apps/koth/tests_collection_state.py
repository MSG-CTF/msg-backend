from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.koth import tests as fixtures
from apps.koth.models import KothChallenge, KothScorePeriod, KothSolve


@override_settings(CACHES=fixtures.LOCMEM, KOTH_LEADERBOARD_CACHE_SECONDS=0)
class CollectionStateTests(TestCase):
    setUp = fixtures.KothApiTests.setUp
    auth = fixtures.KothApiTests.auth

    def responses(self):
        self.auth()
        me = self.client.get("/api/v1/koth/me")
        self.assertEqual(me.status_code, 200)
        progress = next(row for row in me.data["data"]["challenges"]
                        if row["koth_challenge_id"] == str(self.challenge.pk))
        leaderboard = self.client.get("/api/v1/koth/leaderboard", {
            "koth_challenge_id": str(self.challenge.pk),
        })
        self.assertEqual(leaderboard.status_code, 200)
        return progress, leaderboard.data["data"]

    def test_zero_scores_distinguish_no_collection_pending_failure_and_success(self):
        KothSolve.objects.create(team=self.team, challenge=self.challenge, earned_score=0)
        now = timezone.now()
        for status, public in ((None, "NOT_COLLECTED"), ("PENDING", "PENDING"),
                               ("FAILED", "FAILED"), ("APPLIED", "COLLECTED")):
            with self.subTest(status=status):
                KothScorePeriod.objects.filter(challenge=self.challenge).delete()
                if status:
                    KothScorePeriod.objects.create(
                        challenge=self.challenge, period_id=now, status=status,
                        applied_at=now if status == "APPLIED" else None,
                        last_error="private-collector-error",
                        response_payload={"secret": "private-collector-response"},
                    )
                progress, leaderboard = self.responses()
                for data in (progress, leaderboard):
                    self.assertEqual(data["collection_status"], public)
                    self.assertEqual(data["is_stale"], status != "APPLIED")
                    self.assertEqual(data["collection_period_id"], now if status else None)
                    self.assertEqual(data["last_collected_at"], now if status == "APPLIED" else None)
                    self.assertNotIn("private-collector", str(data))
                self.assertEqual(progress["earned_score"], 0)
                self.assertIsNone(progress["rank"])
                self.assertEqual(leaderboard["leaderboard"][0]["earned_score"], 0)
                self.assertIsNone(leaderboard["leaderboard"][0]["rank"])

    def test_later_failure_or_pending_keeps_last_successful_collection_time(self):
        now = timezone.now()
        successful = now - timedelta(minutes=15)
        KothScorePeriod.objects.create(challenge=self.challenge, period_id=successful,
                                       status="APPLIED", applied_at=successful)
        period = KothScorePeriod.objects.create(challenge=self.challenge, period_id=now,
                                                status="FAILED")
        for status in ("FAILED", "PENDING"):
            period.status = status
            period.save(update_fields=["status"])
            for data in self.responses():
                self.assertEqual(data["collection_status"], status)
                self.assertEqual(data["collection_period_id"], now)
                self.assertEqual(data["last_collected_at"], successful)
                self.assertTrue(data["is_stale"])

    def test_late_backfill_does_not_replace_the_latest_period_status(self):
        now = timezone.now()
        KothScorePeriod.objects.create(challenge=self.challenge, period_id=now,
                                       status="FAILED")
        KothScorePeriod.objects.create(challenge=self.challenge, period_id=now - timedelta(minutes=15),
                                       status="APPLIED", applied_at=now - timedelta(seconds=1))
        KothScorePeriod.objects.create(challenge=self.challenge, period_id=now - timedelta(minutes=30),
                                       status="APPLIED", applied_at=now)
        for data in self.responses():
            self.assertEqual(data["collection_status"], "FAILED")
            self.assertEqual(data["collection_period_id"], now)
            self.assertEqual(data["last_collected_at"], now)

    def test_me_collection_metadata_keeps_the_existing_query_budget(self):
        now = timezone.now()
        challenges = list(KothChallenge.objects.all())
        KothScorePeriod.objects.bulk_create([
            KothScorePeriod(challenge=challenge, period_id=now, status="FAILED")
            for challenge in challenges
        ])
        self.auth()
        with self.assertNumQueries(3):
            response = self.client.get("/api/v1/koth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["data"]["challenges"]), len(challenges))
        self.assertTrue(all(row["collection_status"] == "FAILED"
                            for row in response.data["data"]["challenges"]))

    @override_settings(KOTH_LEADERBOARD_CACHE_SECONDS=10)
    def test_old_cached_enum_is_not_served_and_new_contract_is_cached(self):
        cache.set(f"koth:leaderboard:json:v2:{self.challenge.pk}", b'{"old":true}')
        KothScorePeriod.objects.create(challenge=self.challenge, period_id=timezone.now(),
                                       status="APPLIED", applied_at=timezone.now())
        self.auth()
        path = f"/api/v1/koth/leaderboard?koth_challenge_id={self.challenge.pk}"
        first = self.client.get(path, HTTP_ACCEPT="application/json")
        second = self.client.get(path, HTTP_ACCEPT="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["data"]["collection_status"], "COLLECTED")
        self.assertEqual(first.content, second.content)
