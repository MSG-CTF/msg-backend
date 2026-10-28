import json

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team
from apps.common.jwt import hash_token
from apps.koth.models import (
    KothChallenge,
    KothClub,
    KothTeamToken,
    KothTokenVerificationAttempt,
)


class VerifyTeamTokenInputTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.team = Team.objects.create(team_name="verify-input-qa")
        self.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Verify input QA"),
            title="Verify input QA",
            status="ACTIVE",
            open_group=1,
            opened_at=timezone.now(),
            inbound_internal_token_hash=hash_token("input-qa-internal"),
        )
        KothTeamToken.objects.create(
            team=self.team,
            challenge=self.challenge,
            token_hash=hash_token("input-qa-team-token"),
        )

    def post_json(self, payload, internal=None):
        headers = {} if internal is None else {"HTTP_X_INTERNAL_TOKEN": internal}
        return self.client.generic(
            "POST",
            "/internal/koth/team_tokens/verify",
            data=json.dumps(payload),
            content_type="application/json",
            **headers,
        )

    def test_non_object_json_is_rejected_without_server_error_or_failure_audit(self):
        for payload in (None, [], [{}], [{"team_token": "opaque"}], 0, 1, True, "opaque"):
            for internal in (None, "input-qa-internal"):
                with self.subTest(payload=payload, authenticated=bool(internal)):
                    response = self.post_json(payload, internal)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.data["code"], "INVALID_REQUEST")
        self.assertEqual(KothTokenVerificationAttempt.objects.count(), 0)

    def test_invalid_object_fields_are_rejected(self):
        valid = {
            "koth_challenge_id": str(self.challenge.pk),
            "team_token": "input-qa-team-token",
        }
        for payload in (
            {},
            {"team_token": "input-qa-team-token"},
            {**valid, "extra": True},
            {**valid, "team_token": None},
            {**valid, "team_token": {}},
            {"kothChallengeId": str(self.challenge.pk), "teamToken": "opaque"},
        ):
            with self.subTest(payload=payload):
                response = self.post_json(payload, "input-qa-internal")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "INVALID_REQUEST")
        self.assertEqual(KothTokenVerificationAttempt.objects.count(), 0)

    def test_object_payload_still_requires_internal_authentication(self):
        payload = {
            "koth_challenge_id": str(self.challenge.pk),
            "team_token": "input-qa-team-token",
        }
        for internal in (None, "wrong-internal"):
            with self.subTest(internal=internal):
                response = self.post_json(payload, internal)
                self.assertEqual(response.status_code, 401)
        response = self.post_json(payload, "input-qa-internal")
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.data["data"]["valid"], True)
        self.assertEqual(response.data["data"]["team_id"], str(self.team.pk))
