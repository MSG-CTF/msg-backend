import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.common.jwt import hash_token
from apps.koth.models import KothChallenge, KothClub, KothTeamToken
from apps.koth.tokens import build_team_token


class ChallengeScopedTeamTokenTests(TestCase):
    def setUp(self):
        team = Team.objects.create(team_name="qa-scoped-token")
        User.objects.create_user(
            login_id="qa-scoped-user",
            password="local-test-password",
            nickname="qa-scoped-user",
            team=team,
        )
        challenges = []
        for suffix in ("a", "b"):
            club = KothClub.objects.create(name=f"QA scope {suffix}")
            challenge = KothChallenge.objects.create(
                club=club,
                title=f"QA scope {suffix}",
                status="ACTIVE",
                open_group=1,
                opened_at=timezone.now(),
                inbound_internal_token_hash=hash_token(f"internal-scope-{suffix}"),
            )
            challenges.append(challenge)
        client = APIClient()
        login = client.post(
            "/api/v1/auth/login",
            {"login_id": "qa-scoped-user", "password": "local-test-password"},
            format="json",
        )
        assert login.status_code == 200
        jwt = login.data["data"]["access_token"]
        self.team, self.challenges, self.client, self.jwt = team, challenges, client, jwt

    def test_team_token_is_valid_only_for_its_issued_challenge(self):
        team, (first, second), client, jwt = (self.team, self.challenges, self.client, self.jwt)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {jwt}")
        first_issue = client.get(
            f"/api/v1/koth/team_token?koth_challenge_id={first.pk}"
        )
        assert first_issue.status_code == 200
        first_token = first_issue.data["data"]["team_token"]
        client.credentials()

        def verify(challenge, token, credential):
            return client.post(
                "/internal/koth/team_tokens/verify",
                {"koth_challenge_id": str(challenge.pk), "team_token": token},
                format="json",
                HTTP_X_INTERNAL_TOKEN=credential,
            )

        allowed = verify(first, first_token, "internal-scope-a")
        assert allowed.status_code == 200
        assert allowed.data["data"]["valid"] is True
        cross_challenge = verify(second, first_token, "internal-scope-b")
        assert cross_challenge.status_code == 200
        assert cross_challenge.data["data"]["valid"] is False

        client.credentials(HTTP_AUTHORIZATION=f"Bearer {jwt}")
        second_issue = client.get(
            f"/api/v1/koth/team_token?koth_challenge_id={second.pk}"
        )
        assert second_issue.status_code == 200
        second_token = second_issue.data["data"]["team_token"]
        assert second_token != first_token
        assert second_issue.data["data"]["koth_challenge_id"] == str(second.pk)
        assert KothTeamToken.objects.filter(team=team).count() == 2
        client.credentials()
        assert verify(second, second_token, "internal-scope-b").data["data"]["valid"] is True
        assert verify(first, second_token, "internal-scope-a").data["data"]["valid"] is False
        assert verify(first, first_token, "wrong-internal").status_code == 401

    def test_scoped_token_issue_requires_a_real_challenge(self):
        _, (first, _), client, jwt = (self.team, self.challenges, self.client, self.jwt)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {jwt}")
        path = "/api/v1/koth/team_token"
        assert client.get(path).status_code == 400
        assert client.get(f"{path}?koth_challenge_id=not-a-uuid").status_code == 400
        assert client.get(f"{path}?koth_challenge_id={uuid.uuid4()}").status_code == 404
        allowed = client.get(f"{path}?koth_challenge_id={first.pk}")
        assert allowed.status_code == 200
        assert allowed.data["data"]["koth_challenge_id"] == str(first.pk)

    def test_legacy_team_wide_token_is_preserved_but_cannot_verify(self):
        team, (first, _), client, jwt = (self.team, self.challenges, self.client, self.jwt)
        legacy_token = "koth_legacy-test-token"
        legacy = KothTeamToken.objects.create(
            team=team, challenge=None, token_hash=hash_token(legacy_token)
        )

        legacy_result = client.post(
            "/internal/koth/team_tokens/verify",
            {"koth_challenge_id": str(first.pk), "team_token": legacy_token},
            format="json",
            HTTP_X_INTERNAL_TOKEN="internal-scope-a",
        )
        assert legacy_result.status_code == 200
        assert legacy_result.data["data"]["valid"] is False

        client.credentials(HTTP_AUTHORIZATION=f"Bearer {jwt}")
        issued = client.get(f"/api/v1/koth/team_token?koth_challenge_id={first.pk}")
        assert issued.status_code == 200
        assert issued.data["data"]["team_token"] == build_team_token(team.pk, first.pk)
        assert KothTeamToken.objects.filter(pk=legacy.pk, challenge__isnull=True).exists()
        assert KothTeamToken.objects.filter(team=team).count() == 2
