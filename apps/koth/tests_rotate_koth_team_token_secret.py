import uuid
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.accounts.models import Team
from apps.koth.models import KothChallenge, KothChallengeStatus, KothClub, KothTeamToken


def _make_token(idx):
    team = Team.objects.create(team_name=f"rotate-team-{idx}")
    club = KothClub.objects.create(name=f"rotate-club-{idx}")
    challenge = KothChallenge.objects.create(
        club=club,
        title=f"rotate-ch-{idx}",
        open_group=1,
        status=KothChallengeStatus.ACTIVE,
        inbound_internal_token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
    )
    KothTeamToken.objects.create(
        team=team,
        challenge=challenge,
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
    )


class RotateKothTeamTokenSecretTests(TestCase):
    def test_refuses_without_flag(self):
        _make_token(1)
        with self.assertRaises(CommandError):
            call_command("rotate_koth_team_token_secret")
        self.assertEqual(KothTeamToken.objects.count(), 1)

    def test_purges_all_tokens_with_flag(self):
        _make_token(1)
        _make_token(2)
        self.assertEqual(KothTeamToken.objects.count(), 2)
        out = StringIO()
        call_command(
            "rotate_koth_team_token_secret",
            "--yes-i-rotated-the-secret",
            stdout=out,
        )
        self.assertEqual(KothTeamToken.objects.count(), 0)
        self.assertIn("2개", out.getvalue())
