from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.accounts.models import Team, User
from apps.koth.models import KothClub

from .models import (
    SignatureChallenge,
    SignatureFlagSubmission,
    SignatureSolve,
    SignatureSubmissionLock,
)


class SignatureModelTests(TestCase):
    def setUp(self):
        self.club = KothClub.objects.create(name="CodeCure")
        self.team = Team.objects.create(team_name="우리팀")
        self.user = User.objects.create_user(
            login_id="signature-user",
            password="pw1234",
            nickname="시그니처 참가자",
            team=self.team,
        )
        self.challenge = SignatureChallenge.objects.create(
            club=self.club,
            title="CodeCure Signature",
            description="클럽 시그니처 문제",
            flag_hash="hashed-flag",
        )

    def test_challenge_defaults_to_300_points_and_unpublished(self):
        self.assertEqual(self.challenge.score, Decimal("300.00"))
        self.assertFalse(self.challenge.is_published)
        self.assertEqual(str(self.challenge), "CodeCure / CodeCure Signature")

    def test_each_club_accepts_only_one_signature_challenge(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SignatureChallenge.objects.create(
                    club=self.club,
                    title="Duplicate",
                    description="중복 문제",
                    flag_hash="another-hash",
                )

    def test_challenge_score_must_be_positive(self):
        other_club = KothClub.objects.create(name="MJSEC")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SignatureChallenge.objects.create(
                    club=other_club,
                    title="Invalid score",
                    description="잘못된 점수",
                    score=0,
                    flag_hash="hashed-flag",
                )

    def test_team_can_solve_each_challenge_only_once(self):
        SignatureSolve.objects.create(
            team=self.team,
            challenge=self.challenge,
            solved_by_user=self.user,
            earned_score=self.challenge.score,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SignatureSolve.objects.create(
                    team=self.team,
                    challenge=self.challenge,
                    solved_by_user=self.user,
                    earned_score=self.challenge.score,
                )

    def test_solve_score_must_be_positive(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SignatureSolve.objects.create(
                    team=self.team,
                    challenge=self.challenge,
                    solved_by_user=self.user,
                    earned_score=0,
                )

    def test_team_has_only_one_submission_lock_per_challenge(self):
        SignatureSubmissionLock.objects.create(
            team=self.team,
            challenge=self.challenge,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SignatureSubmissionLock.objects.create(
                    team=self.team,
                    challenge=self.challenge,
                )

    def test_submission_stores_hash_and_result(self):
        submission = SignatureFlagSubmission.objects.create(
            team=self.team,
            user=self.user,
            challenge=self.challenge,
            submitted_flag_hash="submitted-hash",
            result=SignatureFlagSubmission.SubmissionResult.INCORRECT,
        )

        self.assertEqual(submission.submitted_flag_hash, "submitted-hash")
        self.assertEqual(
            submission.result,
            SignatureFlagSubmission.SubmissionResult.INCORRECT,
        )
