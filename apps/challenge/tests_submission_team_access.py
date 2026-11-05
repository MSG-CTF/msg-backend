from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.adminpanel.models import AdminEvent
from apps.board.models import Cell, TeamBoardState, TeamChallengeAccess
from apps.challenge.models import Challenge, FlagSubmission, FlagSubmissionLock, Solve
from apps.challenge.services import hash_flag
from apps.common.authentication import JWTAuthentication
from apps.common.jwt import issue_access_token
from apps.koth.models import KothClub
from apps.signature.models import (
    SignatureChallenge,
    SignatureFlagSubmission,
    SignatureSolve,
    SignatureSubmissionLock,
)
from apps.teams.models import MileageHistory


class SubmissionTeamFixtures:
    signature = False

    def setUp(self):
        self.team = Team.objects.create(team_name="submission-team")
        self.user = User.objects.create_user(login_id="submitter", team=self.team)
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.user)
        )
        if self.signature:
            club = KothClub.objects.create(name="submission-club")
            self.challenge = SignatureChallenge.objects.create(
                club=club,
                title="Signature",
                description="Submit",
                score=300,
                flag_hash=hash_flag("local-submission-flag"),
                is_published=True,
            )
            self.solve_model = SignatureSolve
            self.lock_model = SignatureSubmissionLock
            self.url = f"/api/v1/signatures/{self.challenge.pk}/submit"
        else:
            self.challenge = Challenge.objects.create(
                title="Challenge",
                category="WEB",
                difficulty="EASY",
                score=500,
                flag_hash=hash_flag("local-submission-flag"),
                is_published=True,
            )
            cell = Cell.objects.create(cell_index=1, type="CHALLENGE", name="cell")
            TeamChallengeAccess.objects.create(
                team=self.team,
                challenge=self.challenge,
                source_cell=cell,
            )
            self.solve_model = Solve
            self.lock_model = FlagSubmissionLock
            self.url = f"/api/v1/challenges/{self.challenge.pk}/submit"

    def submit(self, client=None, flag="local-submission-flag"):
        return (client or self.client).post(self.url, {"flag": flag}, format="json")


class SubmissionAccessChecks(SubmissionTeamFixtures):
    def game_state(self):
        models = (
            Solve,
            FlagSubmission,
            FlagSubmissionLock,
            SignatureSolve,
            SignatureFlagSubmission,
            SignatureSubmissionLock,
            TeamBoardState,
            TeamChallengeAccess,
            MileageHistory,
            AdminEvent,
        )
        return [list(model.objects.order_by("pk").values()) for model in models] + [
            list(Team.objects.order_by("pk").values("pk", "mileage", "team_score"))
        ]

    def reject_changed_access(
        self, change, *, flag="local-submission-flag", code="FORBIDDEN"
    ):
        before = self.game_state()
        check = JWTAuthentication._check_banned

        def authenticate_then_change(auth, request, user):
            check(auth, request, user)
            change()

        with patch.object(JWTAuthentication, "_check_banned", authenticate_then_change):
            response = self.submit(flag=flag)
        self.assertEqual((response.status_code, response.data["code"]), (403, code))
        self.assertEqual(self.game_state(), before)

    def ban(self):
        Team.objects.filter(pk=self.team.pk).update(is_banned=True)

    def remove(self):
        User.objects.filter(pk=self.user.pk).update(team=None)

    def test_ban_after_auth_rejects_correct_submission_without_writes(self):
        self.reject_changed_access(self.ban, code="TEAM_BANNED")

    def test_ban_after_auth_rejects_wrong_submission_without_writes(self):
        self.reject_changed_access(self.ban, flag="wrong", code="TEAM_BANNED")

    def test_removed_member_cannot_solve_for_previous_team(self):
        self.reject_changed_access(self.remove)

    def test_moved_member_cannot_solve_for_previous_team(self):
        other = Team.objects.create(team_name="other-submission-team")
        self.reject_changed_access(
            lambda: User.objects.filter(pk=self.user.pk).update(team=other)
        )

    def test_ban_is_checked_before_recording_locked_submission(self):
        self.lock_model.objects.create(
            team=self.team,
            challenge=self.challenge,
            failed_count=3,
            locked_until=timezone.now() + timedelta(minutes=1),
        )
        self.reject_changed_access(self.ban, code="TEAM_BANNED")

    def test_membership_is_checked_before_recording_locked_submission(self):
        self.lock_model.objects.create(
            team=self.team,
            challenge=self.challenge,
            failed_count=3,
            locked_until=timezone.now() + timedelta(minutes=1),
        )
        self.reject_changed_access(self.remove)

    def test_membership_is_checked_before_already_solved_response(self):
        self.solve_model.objects.create(
            team=self.team,
            challenge=self.challenge,
            solved_by_user=self.user,
            earned_score=300,
            **({} if self.signature else {"earned_mileage": 0}),
        )
        self.reject_changed_access(self.remove)

    def test_unchanged_membership_still_allows_submission(self):
        response = self.submit()
        self.assertEqual(
            (response.status_code, response.data["code"]), (200, "SUCCESS")
        )
        self.assertEqual(self.solve_model.objects.filter(team=self.team).count(), 1)


@override_settings(SECURE_SSL_REDIRECT=False)
class ChallengeSubmissionAccessTests(SubmissionAccessChecks, TestCase):
    pass


@override_settings(SECURE_SSL_REDIRECT=False)
class SignatureSubmissionAccessTests(SubmissionAccessChecks, TestCase):
    signature = True


class SubmissionTeamRaces(SubmissionTeamFixtures):
    def race(self, action, *, admin_first):
        admin = User.objects.create_user(login_id="submission-admin", role=Role.ADMIN)
        tokens = {
            "admin": issue_access_token(admin),
            "submit": issue_access_token(self.user),
        }
        held, waiting = Event(), Event()
        leader = "admin" if admin_first else "submit"

        def request(kind):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                client = APIClient(raise_request_exception=False)
                client.credentials(HTTP_AUTHORIZATION="Bearer " + tokens[kind])
                coordinated = False

                def coordinate(execute, sql, params, many, context):
                    nonlocal coordinated
                    team_lock = 'FROM "teams"' in sql and "FOR " in sql
                    if coordinated or not team_lock:
                        return execute(sql, params, many, context)
                    coordinated = True
                    if kind != leader:
                        waiting.set()
                        return execute(sql, params, many, context)
                    result = execute(sql, params, many, context)
                    held.set()
                    if not waiting.wait(8):
                        raise AssertionError(
                            "Competing request did not reach the team lock"
                        )
                    return result

                with connection.execute_wrapper(coordinate):
                    if kind == "submit":
                        response = self.submit(client)
                    elif action == "remove":
                        response = client.patch(
                            f"/api/v1/admin/teams/{self.team.pk}",
                            {
                                "remove_user_ids": [str(self.user.pk)],
                                "reason": "Local race test",
                            },
                            format="json",
                        )
                    else:
                        response = client.post(
                            f"/api/v1/admin/teams/{self.team.pk}/ban",
                            {"ban_reason": "Local race test"},
                            format="json",
                        )
                return response.status_code, response.data
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(request, leader)
            self.assertTrue(held.wait(8), "First request did not acquire the team lock")
            follower = "submit" if admin_first else "admin"
            second = pool.submit(request, follower)
            results = {leader: first.result(15), follower: second.result(15)}
        self.assertEqual(results["admin"][0], 200, results)
        status, body = results["submit"]
        if admin_first:
            self.assertEqual(
                (status, body["code"]),
                (
                    403,
                    "FORBIDDEN" if action == "remove" else "TEAM_BANNED",
                ),
                results,
            )
            self.assertFalse(self.solve_model.objects.filter(team=self.team).exists())
            self.assertFalse(self.lock_model.objects.exists())
            self.assertFalse(FlagSubmission.objects.exists())
            self.assertFalse(SignatureFlagSubmission.objects.exists())
            self.assertFalse(MileageHistory.objects.exists())
        else:
            self.assertEqual((status, body["code"]), (200, "SUCCESS"), results)
            self.assertEqual(self.solve_model.objects.filter(team=self.team).count(), 1)
        self.user.refresh_from_db()
        self.team.refresh_from_db()
        if action == "remove":
            self.assertIsNone(self.user.team_id)
        else:
            self.assertTrue(self.team.is_banned)

    def test_ban_committed_before_submission_is_rejected(self):
        self.race("ban", admin_first=True)

    def test_removal_committed_before_submission_is_rejected(self):
        self.race("remove", admin_first=True)

    def test_submission_finishes_before_later_ban(self):
        self.race("ban", admin_first=False)

    def test_submission_finishes_before_later_removal(self):
        self.race("remove", admin_first=False)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
@override_settings(SECURE_SSL_REDIRECT=False)
class ConcurrentChallengeTeamAccessTests(SubmissionTeamRaces, TransactionTestCase):
    pass


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
@override_settings(SECURE_SSL_REDIRECT=False)
class ConcurrentSignatureTeamAccessTests(SubmissionTeamRaces, TransactionTestCase):
    signature = True
