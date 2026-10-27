from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.common.jwt import hash_token
from apps.koth.models import KothChallenge, KothClub


class ClosedChallengeTokenTests(TestCase):
    def test_team_token_is_accepted_only_while_challenge_is_active(self):
        team = Team.objects.create(team_name="qa-closed-token")
        User.objects.create_user(
            login_id="qa-closed-token-user",
            password="local-test-password",
            nickname="qa-token-user",
            team=team,
        )
        club = KothClub.objects.create(name="QA token state")
        challenge = KothChallenge.objects.create(
            club=club,
            title="QA token state",
            status="ACTIVE",
            open_group=1,
            opened_at=timezone.now(),
            inbound_internal_token_hash=hash_token("local-internal-credential"),
        )
        client = APIClient()
        login = client.post(
            "/api/v1/auth/login",
            {"login_id": "qa-closed-token-user", "password": "local-test-password"},
            format="json",
        )
        assert login.status_code == 200
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access_token']}")
        issued = client.get("/api/v1/koth/team_token")
        assert issued.status_code == 200
        raw_token = issued.data["data"]["team_token"]
        client.credentials()

        body = {"koth_challenge_id": str(challenge.pk), "team_token": raw_token}
        path = "/internal/koth/team_tokens/verify"
        internal = {"HTTP_X_INTERNAL_TOKEN": "local-internal-credential"}
        active = client.post(path, body, format="json", **internal)
        assert active.status_code == 200
        assert active.data["data"]["valid"] is True

        challenge.status = "CLOSED"
        challenge.closed_at = timezone.now()
        challenge.save(update_fields=["status", "closed_at"])
        closed = client.post(path, body, format="json", **internal)
        assert closed.status_code == 200
        assert closed.data["data"]["valid"] is False

        challenge.status = "ACTIVE"
        challenge.closed_at = None
        challenge.save(update_fields=["status", "closed_at"])
        team.is_banned = True
        team.save(update_fields=["is_banned"])
        banned = client.post(path, body, format="json", **internal)
        assert banned.status_code == 200
        assert banned.data["data"]["valid"] is False

        wrong_internal = client.post(
            path, body, format="json", HTTP_X_INTERNAL_TOKEN="wrong-credential"
        )
        assert wrong_internal.status_code == 401
