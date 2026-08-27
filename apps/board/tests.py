from datetime import timedelta
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.response import Response

from apps.accounts.models import Team, User
from apps.board.models import (
    Cell,
    ChanceCard,
    PendingDiceRoll,
    QuarantineEscapeCode,
    TeamBoardState,
    TeamCellCandidate,
    TeamCellConsumption,
    TeamChallengeAccess,
    TeamChanceCard,
)
from apps.board.services import SOLVE_LIMIT_SECONDS, get_or_create_board_state
from apps.board.idempotency import idempotent
from apps.challenge.models import Challenge, OpenedChallenge
from apps.teams.models import MileageHistory

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class IdempotencyConcurrencyTestCase(SimpleTestCase):
    def test_same_key_concurrent_requests_execute_view_once_and_replay_response(self):
        calls = []

        class ProbeView:
            @idempotent
            def post(self, request):
                calls.append(request.body)
                time.sleep(0.2)
                return Response({"value": len(calls)}, status=200)

        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        requests = [
            factory.post(
                "/api/v1/board/dice/roll",
                {"roll": True},
                format="json",
                HTTP_IDEMPOTENCY_KEY="parallel-key",
            )
            for _ in range(2)
        ]
        for request in requests:
            request.user = SimpleNamespace(user_id=1)

        cache.clear()
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(ProbeView().post, requests))

        self.assertEqual(len(calls), 1)
        self.assertEqual(responses[0].data, responses[1].data)


@override_settings(DEBUG=False)
class BoardDebugRouteTestCase(SimpleTestCase):
    def test_debug_routes_and_dashboard_are_not_exposed_when_debug_is_disabled(self):
        self.assertEqual(self.client.get("/").status_code, 404)

        for path in ("/board/_debug/solve", "/board/_debug/release_quarantine"):
            with self.subTest(path=path):
                response = self.client.post(path, data={}, content_type="application/json")
                self.assertEqual(response.status_code, 404)


@override_settings(CACHES=LOCMEM)
class BoardApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        call_command("seed_board", verbosity=0)

        self.client = APIClient()
        self.team = Team.objects.create(team_name="우리팀")
        self.other_team = Team.objects.create(team_name="다른팀")
        User.objects.create_user(
            login_id="leader", password="pw1234", nickname="팀장", team=self.team, is_leader=True
        )
        User.objects.create_user(
            login_id="member", password="pw1234", nickname="팀원", team=self.team, is_leader=False
        )
        User.objects.create_user(
            login_id="other_leader", password="pw1234", nickname="다른팀장",
            team=self.other_team, is_leader=True,
        )

        self.state = get_or_create_board_state(self.team)
        self.as_leader()

    def _login(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login", {"login_id": login_id, "password": "pw1234"}, format="json"
        )
        return res.data["data"]["access_token"]

    def as_leader(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._login('leader')}")

    def as_member(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._login('member')}")

    def as_other_leader(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._login('other_leader')}")

    def post_idem(self, path, data=None, key="idem-1"):
        return self.client.post(path, data or {}, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def set_position(self, cell_index, consumed=False):
        self.state.position_id = cell_index
        self.state.save(update_fields=["position"])
        if consumed:
            TeamCellConsumption.objects.get_or_create(team=self.team, cell_id=cell_index)
        return Cell.objects.get(cell_index=cell_index)

    def draw_card(self, card_id, source_cell_index=7):
        return TeamChanceCard.objects.create(
            team=self.team,
            source_cell_id=source_cell_index,
            card=ChanceCard.objects.get(card_id=card_id),
        )

    def reset_card_scenario(self):
        PendingDiceRoll.objects.filter(team=self.team).delete()
        TeamChanceCard.objects.filter(team=self.team).delete()
        TeamCellConsumption.objects.filter(team=self.team).delete()
        TeamCellCandidate.objects.filter(team=self.team).delete()
        TeamChallengeAccess.objects.filter(team=self.team).delete()
        self.state.refresh_from_db()
        self.state.position_id = 1
        self.state.dice_rolls_left = 1
        self.state.active_challenge_access = None
        self.state.is_quarantined = False
        self.state.next_dice_reset_at = None
        self.state.quarantine_released_at = None
        self.state.airport_move_used = False
        self.state.has_passed_start = False
        self.state.save(
            update_fields=[
                "position",
                "dice_rolls_left",
                "active_challenge_access",
                "is_quarantined",
                "next_dice_reset_at",
                "quarantine_released_at",
                "airport_move_used",
                "has_passed_start",
                "updated_at",
            ]
        )

    def mark_challenge_solved(self, cell):
        if cell.type != Cell.CellType.CHALLENGE:
            return
        challenge = Challenge.objects.filter(difficulty=cell.difficulty).first()
        TeamChallengeAccess.objects.update_or_create(
            team=self.team,
            source_cell=cell,
            defaults={
                "challenge": challenge,
                "status": TeamChallengeAccess.Status.CLEARED,
                "cleared_at": timezone.now(),
            },
        )

    # ---------------------------------------------------------------- GET /board

    def test_board_returns_notion_shape(self):
        self.client.credentials()
        response = self.client.get("/api/v1/board")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], "SUCCESS")
        self.assertEqual(body["data"]["total_cell_count"], 36)
        self.assertEqual(len(body["data"]["cells"]), 36)

    def test_board_returns_load_failed_when_not_seeded(self):
        TeamBoardState.objects.all().delete()
        Cell.objects.all().delete()
        self.client.credentials()

        response = self.client.get("/api/v1/board")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "BOARD_LOAD_FAILED")

    # ---------------------------------------------------------------- GET /board/me

    def test_board_me_requires_auth(self):
        self.client.credentials()
        response = self.client.get("/api/v1/board/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "TOKEN_MISSING")

    def test_board_me_returns_initial_state(self):
        response = self.client.get("/api/v1/board/me")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["position"], 1)
        self.assertEqual(data["type"], Cell.CellType.START)
        self.assertEqual(data["dice_rolls_left"], 1)
        self.assertFalse(data["board_completed"])
        self.assertEqual(data["consumed_cell_indexes"], [])
        self.assertEqual(data["cell_states"], [])
        self.assertEqual(data["chance_cards"], [])
        self.assertIsNone(data["active_challenge"])
        self.assertEqual(
            set(data.keys()),
            {
                "position", "type", "is_quarantined", "dice_rolls_left", "next_dice_reset_at",
                "quarantine_attempts_left", "airport_move_used", "has_passed_start",
                "board_completed", "consumed_cell_indexes", "cell_states", "chance_cards",
                "active_challenge",
            },
        )

    def test_board_me_isolates_other_team(self):
        TeamCellConsumption.objects.create(team=self.team, cell_id=1)
        self.as_other_leader()

        response = self.client.get("/api/v1/board/me")

        self.assertEqual(response.json()["data"]["consumed_cell_indexes"], [])

    def test_board_me_active_challenge_includes_solve_deadline_and_remaining_seconds(self):
        # 새로고침해도 남은 시간을 다시 계산해서 내려줘야 한다 (cell/open 응답에만 있으면 새로고침 시 유실).
        cell = self.set_position(2, consumed=True)
        challenge = Challenge.objects.filter(difficulty=cell.difficulty).first()
        access = TeamChallengeAccess.objects.create(team=self.team, challenge=challenge, source_cell=cell)
        self.state.active_challenge_access = access
        self.state.save(update_fields=["active_challenge_access"])

        response = self.client.get("/api/v1/board/me")

        active_challenge = response.json()["data"]["active_challenge"]
        self.assertEqual(active_challenge["challenge_id"], str(challenge.challenge_id))
        self.assertEqual(
            active_challenge["solve_deadline_at"],
            (access.opened_at + timedelta(seconds=SOLVE_LIMIT_SECONDS)).isoformat().replace("+00:00", "Z"),
        )
        self.assertGreater(active_challenge["remaining_seconds"], 0)
        self.assertLessEqual(active_challenge["remaining_seconds"], SOLVE_LIMIT_SECONDS)

    def test_board_me_active_challenge_remaining_seconds_floors_at_zero_past_deadline(self):
        cell = self.set_position(2, consumed=True)
        challenge = Challenge.objects.filter(difficulty=cell.difficulty).first()
        access = TeamChallengeAccess.objects.create(team=self.team, challenge=challenge, source_cell=cell)
        access.opened_at = timezone.now() - timedelta(seconds=SOLVE_LIMIT_SECONDS + 30)
        access.save(update_fields=["opened_at"])
        self.state.active_challenge_access = access
        self.state.save(update_fields=["active_challenge_access"])

        response = self.client.get("/api/v1/board/me")

        self.assertEqual(response.json()["data"]["active_challenge"]["remaining_seconds"], 0)

    # ---------------------------------------------------------------- GET /board/dice/status

    def test_dice_status_initial(self):
        response = self.client.get("/api/v1/board/dice/status")

        data = response.json()["data"]
        self.assertTrue(data["can_roll"])
        self.assertIsNone(data["blocked_reason"])
        self.assertEqual(data["dice_rolls_left"], 1)

    def test_dice_status_challenge_not_selected(self):
        self.set_position(2, consumed=True)  # 시드 결과 기준 CHALLENGE 칸

        response = self.client.get("/api/v1/board/dice/status")

        self.assertEqual(response.json()["data"]["blocked_reason"], "CHALLENGE_NOT_SELECTED")

    def test_dice_status_timer_running(self):
        cell = self.set_position(2, consumed=True)
        challenge = Challenge.objects.filter(difficulty=cell.difficulty).first()
        TeamChallengeAccess.objects.create(team=self.team, challenge=challenge, source_cell=cell)

        response = self.client.get("/api/v1/board/dice/status")

        self.assertEqual(response.json()["data"]["blocked_reason"], "TIMER_RUNNING")

    def test_dice_status_timer_running_releases_after_solve_deadline(self):
        # 15분이 지나면 문제를 풀지 않았어도 TIMER_RUNNING이 풀려야 한다 (미해결 시 영구 차단 회귀 방지).
        cell = self.set_position(2, consumed=True)
        challenge = Challenge.objects.filter(difficulty=cell.difficulty).first()
        access = TeamChallengeAccess.objects.create(team=self.team, challenge=challenge, source_cell=cell)
        access.opened_at = timezone.now() - timedelta(seconds=SOLVE_LIMIT_SECONDS + 1)
        access.save(update_fields=["opened_at"])

        response = self.client.get("/api/v1/board/dice/status")

        data = response.json()["data"]
        self.assertIsNone(data["blocked_reason"])
        self.assertTrue(data["can_roll"])

    def test_dice_roll_succeeds_after_challenge_timer_expires(self):
        cell = self.set_position(2, consumed=True)
        challenge = Challenge.objects.filter(difficulty=cell.difficulty).first()
        access = TeamChallengeAccess.objects.create(team=self.team, challenge=challenge, source_cell=cell)
        access.opened_at = timezone.now() - timedelta(seconds=SOLVE_LIMIT_SECONDS + 1)
        access.save(update_fields=["opened_at"])

        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            response = self.post_idem("/api/v1/board/dice/roll")

        self.assertEqual(response.status_code, 200)

    def test_dice_status_quarantined(self):
        self.state.is_quarantined = True
        self.state.save(update_fields=["is_quarantined"])

        response = self.client.get("/api/v1/board/dice/status")

        data = response.json()["data"]
        self.assertEqual(data["blocked_reason"], "QUARANTINED")
        self.assertTrue(data["is_quarantined"])

    def test_dice_status_board_completed(self):
        for cell_index in range(1, 37):
            TeamCellConsumption.objects.create(team=self.team, cell_id=cell_index)

        response = self.client.get("/api/v1/board/dice/status")

        self.assertEqual(response.json()["data"]["blocked_reason"], "BOARD_COMPLETED")

    # ---------------------------------------------------------------- POST /board/dice/roll

    def test_dice_roll_requires_idempotency_key(self):
        response = self.client.post("/api/v1/board/dice/roll")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "IDEMPOTENCY_KEY_REQUIRED")

    def test_dice_roll_forbidden_for_non_leader(self):
        self.as_member()
        response = self.post_idem("/api/v1/board/dice/roll")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "NOT_TEAM_LEADER")

    def test_dice_roll_replays_response_for_same_idempotency_key(self):
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            first = self.post_idem("/api/v1/board/dice/roll", key="same-key")
        second = self.post_idem("/api/v1/board/dice/roll", key="same-key")

        self.assertEqual(first.json(), second.json())
        self.assertEqual(TeamBoardState.objects.get(team=self.team).dice_rolls_left, 0)

    def test_dice_roll_moves_team_and_consumes_cell(self):
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            response = self.post_idem("/api/v1/board/dice/roll")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["rolled_number"], 2)
        self.assertEqual(data["previous_position"], 1)
        self.assertEqual(data["current_position"], 3)
        self.assertEqual(data["movement_path"], [2, 3])
        self.assertFalse(data["pending_confirm"])
        self.assertIsNone(data["usable_chance_card"])
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 3)
        self.assertEqual(self.state.dice_rolls_left, 0)
        self.assertTrue(TeamCellConsumption.objects.filter(team=self.team, cell_id=3).exists())

    def test_dice_roll_skips_consumed_cells(self):
        # 물리 거리(1+1=2칸)의 최종 도착지(3번 칸)가 이미 소모된 경우에만 다음 칸으로 넘어간다.
        TeamCellConsumption.objects.create(team=self.team, cell_id=3)
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            response = self.post_idem("/api/v1/board/dice/roll")

        data = response.json()["data"]
        self.assertEqual(data["current_position"], 4)
        self.assertEqual(data["skipped_cells"], [3])
        self.assertEqual(data["movement_path"], [2, 3, 4])

    def test_dice_roll_landing_on_start_grants_mileage_and_roll(self):
        cell = self.set_position(35)
        challenge = Challenge.objects.filter(difficulty=cell.difficulty).first()
        TeamChallengeAccess.objects.create(
            team=self.team, challenge=challenge, source_cell=cell,
            status=TeamChallengeAccess.Status.CLEARED, cleared_at=timezone.now(),
        )
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            response = self.post_idem("/api/v1/board/dice/roll")

        data = response.json()["data"]
        self.assertEqual(data["current_position"], 1)
        self.assertTrue(data["passed_start"])
        self.assertEqual(data["start_reward"], {"mileage_gained": 100, "roll_gained": 1})
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)
        self.assertEqual(
            list(MileageHistory.objects.filter(team=self.team).values_list("type", "amount")),
            [("START_BONUS", 100)],
        )

    def test_dice_roll_blocked_when_no_roll_left(self):
        self.state.dice_rolls_left = 0
        self.state.save(update_fields=["dice_rolls_left"])
        response = self.post_idem("/api/v1/board/dice/roll")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "NO_ROLL_LEFT")

    def test_dice_roll_blocked_by_challenge_not_selected(self):
        self.set_position(2, consumed=True)
        response = self.post_idem("/api/v1/board/dice/roll")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "CHALLENGE_NOT_SELECTED")

    def test_dice_roll_blocked_while_quarantined(self):
        self.state.is_quarantined = True
        self.state.save(update_fields=["is_quarantined"])
        response = self.post_idem("/api/v1/board/dice/roll")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "QUARANTINED")

    def test_dice_roll_creates_pending_when_post_roll_card_held(self):
        self.draw_card("card_reroll")
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            response = self.post_idem("/api/v1/board/dice/roll")

        data = response.json()["data"]
        self.assertTrue(data["pending_confirm"])
        self.assertEqual(data["usable_chance_card"], {"card_id": "card_reroll", "effect": "RE_ROLL"})
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 1)  # 아직 확정 전
        self.assertTrue(PendingDiceRoll.objects.filter(team=self.team).exists())
        self.assertFalse(TeamCellConsumption.objects.filter(team=self.team, cell_id=3).exists())

    def test_dice_roll_blocked_while_previous_roll_still_pending(self):
        self.draw_card("card_reroll")
        self.state.dice_rolls_left = 2
        self.state.save(update_fields=["dice_rolls_left"])
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            self.post_idem("/api/v1/board/dice/roll", key="roll-1")

        response = self.post_idem("/api/v1/board/dice/roll", key="roll-2")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "PENDING_CONFIRM")
        # 두 번째 시도가 막혔으니 주사위는 첫 번째 굴림만큼만 소모돼야 한다
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 1)
        self.assertEqual(PendingDiceRoll.objects.filter(team=self.team).count(), 1)

    def test_dice_status_reports_pending_confirm_as_blocked_reason(self):
        self.draw_card("card_reroll")
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            self.post_idem("/api/v1/board/dice/roll")

        response = self.client.get("/api/v1/board/dice/status")

        data = response.json()["data"]
        self.assertFalse(data["can_roll"])
        self.assertEqual(data["blocked_reason"], "PENDING_CONFIRM")

    # ---------------------------------------------------------------- POST /board/dice/confirm

    def test_dice_confirm_without_pending_fails(self):
        response = self.post_idem("/api/v1/board/dice/confirm")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "NO_PENDING_ROLL")

    def test_dice_confirm_finalizes_pending_roll(self):
        self.draw_card("card_reroll")
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            self.post_idem("/api/v1/board/dice/roll")

        response = self.post_idem("/api/v1/board/dice/confirm")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["current_position"], 3)
        self.assertFalse(data["pending_confirm"])
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 3)
        self.assertFalse(PendingDiceRoll.objects.filter(team=self.team).exists())
        self.assertTrue(TeamCellConsumption.objects.filter(team=self.team, cell_id=3).exists())

    # ---------------------------------------------------------------- POST /board/airport/move

    def test_airport_move_success(self):
        self.set_position(21)
        response = self.post_idem("/api/v1/board/airport/move", {"destination_index": 5})

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["previous_position"], 21)
        self.assertEqual(data["current_position"], 5)
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 5)
        self.assertTrue(self.state.airport_move_used)

    def test_airport_move_rejects_consumed_destination(self):
        self.set_position(21)
        TeamCellConsumption.objects.create(team=self.team, cell_id=5)

        response = self.post_idem("/api/v1/board/airport/move", {"destination_index": 5})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_DESTINATION_INDEX")

    def test_airport_move_rejects_out_of_range(self):
        self.set_position(21)
        response = self.post_idem("/api/v1/board/airport/move", {"destination_index": 99})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_DESTINATION_INDEX")

    def test_airport_move_requires_airport_cell(self):
        response = self.post_idem("/api/v1/board/airport/move", {"destination_index": 5})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "NOT_AIRPORT_CELL")

    def test_airport_move_cannot_be_used_twice(self):
        self.set_position(21)
        self.post_idem("/api/v1/board/airport/move", {"destination_index": 5}, key="k1")
        self.set_position(21)

        response = self.post_idem("/api/v1/board/airport/move", {"destination_index": 6}, key="k2")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "AIRPORT_MOVE_ALREADY_USED")

    def test_airport_move_forbidden_for_non_leader(self):
        self.set_position(21)
        self.as_member()
        response = self.post_idem("/api/v1/board/airport/move", {"destination_index": 5})
        self.assertEqual(response.status_code, 403)

    # ---------------------------------------------------------------- cell/current, cell/open

    def test_cell_current_offers_three_candidates_on_challenge_cell(self):
        cell = self.set_position(2, consumed=True)

        response = self.client.get("/api/v1/board/cell/current")

        data = response.json()["data"]
        self.assertEqual(data["cell_index"], cell.cell_index)
        self.assertEqual(len(data["challenge_candidates"]), 3)

    def test_cell_current_empty_candidates_for_non_challenge_cell(self):
        response = self.client.get("/api/v1/board/cell/current")
        self.assertEqual(response.json()["data"]["challenge_candidates"], [])

    def test_cell_current_blocked_while_pending_roll(self):
        self.draw_card("card_reroll")
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            self.post_idem("/api/v1/board/dice/roll")

        response = self.client.get("/api/v1/board/cell/current")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "PENDING_CONFIRM")

    def test_cell_open_blocked_while_pending_roll(self):
        self.draw_card("card_reroll")
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            self.post_idem("/api/v1/board/dice/roll")
        challenge = Challenge.objects.first()

        response = self.post_idem(
            "/api/v1/board/cell/open", {"challenge_id": str(challenge.challenge_id)}, key="open-pending"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "PENDING_CONFIRM")

    def test_cell_open_requires_challenge_id(self):
        self.set_position(2, consumed=True)
        response = self.post_idem("/api/v1/board/cell/open", {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "CHALLENGE_ID_REQUIRED")

    def test_cell_open_rejects_non_candidate(self):
        self.set_position(2, consumed=True)
        other_challenge = Challenge.objects.exclude(
            difficulty=Cell.objects.get(cell_index=2).difficulty
        ).first()

        response = self.post_idem(
            "/api/v1/board/cell/open", {"challenge_id": str(other_challenge.challenge_id)}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "CHALLENGE_NOT_CANDIDATE")

    def test_cell_open_success_and_double_open_blocked(self):
        self.set_position(2, consumed=True)
        current = self.client.get("/api/v1/board/cell/current").json()["data"]
        challenge_id = current["challenge_candidates"][0]["challenge_id"]

        response = self.post_idem(
            "/api/v1/board/cell/open", {"challenge_id": challenge_id}, key="open-1"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["challenge_id"], challenge_id)

        second = self.post_idem(
            "/api/v1/board/cell/open", {"challenge_id": challenge_id}, key="open-2"
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "CELL_ALREADY_OPENED")

    def test_board_open_challenge_submit_and_board_completion_are_one_flow(self):
        cell = self.set_position(2, consumed=True)
        current = self.client.get("/api/v1/board/cell/current").json()["data"]
        challenge_id = current["challenge_candidates"][0]["challenge_id"]
        challenge = Challenge.objects.get(challenge_id=challenge_id)

        opened = self.post_idem(
            "/api/v1/board/cell/open", {"challenge_id": challenge_id}, key="integrated-open"
        )
        self.assertEqual(opened.status_code, 200)
        self.assertTrue(
            OpenedChallenge.objects.filter(team=self.team, challenge=challenge).exists()
        )

        detail = self.client.get(f"/api/v1/challenges/{challenge_id}")
        self.assertEqual(detail.status_code, 200)

        challenge_number = challenge.board_meta.challenge_number
        submit = self.client.post(
            f"/api/v1/challenges/{challenge_id}/submit",
            {"flag": f"MSG{{challenge_{challenge_number:02d}}}"},
            format="json",
        )
        self.assertEqual(submit.status_code, 200)
        self.assertEqual(submit.json()["data"]["earned_mileage"], 120)

        access = TeamChallengeAccess.objects.get(team=self.team, challenge=challenge)
        self.assertEqual(access.status, TeamChallengeAccess.Status.CLEARED)
        self.state.refresh_from_db()
        self.assertIsNone(self.state.active_challenge_access_id)

    def test_cell_open_requires_challenge_cell(self):
        response = self.post_idem("/api/v1/board/cell/open", {"challenge_id": "not-a-uuid"})
        self.assertEqual(response.status_code, 400)

    # ---------------------------------------------------------------- opened_challenges

    def test_opened_challenges_reflects_solved_state(self):
        cell = self.set_position(2, consumed=True)
        challenge = Challenge.objects.filter(difficulty=cell.difficulty).first()
        access = TeamChallengeAccess.objects.create(
            team=self.team, challenge=challenge, source_cell=cell,
            status=TeamChallengeAccess.Status.CLEARED, cleared_at=timezone.now(),
        )

        response = self.client.get("/api/v1/board/opened_challenges")

        data = response.json()["data"]
        self.assertEqual(data["total_count"], 1)
        self.assertEqual(data["solved_count"], 1)
        self.assertEqual(data["opened_challenges"][0]["challenge_id"], str(access.challenge_id))
        self.assertTrue(data["opened_challenges"][0]["is_solved"])

    # ---------------------------------------------------------------- chance/catalog

    def test_chance_catalog_returns_seven_cards_without_auth(self):
        self.client.credentials()
        response = self.client.get("/api/v1/board/chance/catalog")

        self.assertEqual(set(response.json()), {"code", "message", "data"})
        data = response.json()["data"]
        self.assertEqual(data["total_count"], 7)
        cards = {
            card["card_id"]: (card["name"], card["effect"], card["usage_timing"])
            for card in data["cards"]
        }
        self.assertEqual(
            cards,
            {
                "card_reroll": ("주사위 다시 굴리기", "RE_ROLL", "POST_ROLL"),
                "card_roll_twice_choose": ("주사위 2회 굴림 후 선택", "ROLL_TWICE_CHOOSE", "PRE_ROLL"),
                "card_move_offset": ("주변 칸 이동", "MOVE_OFFSET", "POST_ROLL"),
                "card_free_travel": ("세계여행", "FREE_MOVE", "PRE_ROLL"),
                "card_extra_roll": ("주사위 보너스", "GRANT_EXTRA_ROLL", "PRE_ROLL"),
                "card_quarantine_defense": ("무인도 방어", "QUARANTINE_ESCAPE_FREE", "QUARANTINE_STATE"),
                "card_move_to_quarantine": ("무인도 이동", "FORCE_MOVE_TO_QUARANTINE", "PRE_ROLL"),
            },
        )
        roll_twice = next(card for card in data["cards"] if card["card_id"] == "card_roll_twice_choose")
        self.assertEqual(roll_twice["usage_timing"], "PRE_ROLL")

    # ---------------------------------------------------------------- chance/now

    def test_chance_now_draws_card_and_grants_roll(self):
        # consumed=True: 실제 플레이에서는 도착 즉시 칸이 소모된다 (finalize_landing).
        # 소모 여부와 "카드를 뽑았는지"는 별개 신호여야 한다 — 회귀 테스트.
        self.set_position(7, consumed=True)

        with patch("apps.board.services.random.choice") as choice_mock:
            choice_mock.side_effect = lambda seq: seq[0]
            response = self.post_idem("/api/v1/board/chance/now")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["dice_rolls_left"], 2)
        self.assertTrue(TeamCellConsumption.objects.filter(team=self.team, cell_id=7).exists())

    def test_chance_now_clears_stale_dice_reset_timer(self):
        # chance/now의 +1 지급도 grant_dice_roll을 거치므로 대기 중인 회복 타이머를 지워야 한다.
        self.set_position(7, consumed=True)
        self.state.dice_rolls_left = 0
        self.state.next_dice_reset_at = timezone.now() + timedelta(minutes=15)
        self.state.save(update_fields=["dice_rolls_left", "next_dice_reset_at"])

        with patch("apps.board.services.random.choice") as choice_mock:
            choice_mock.side_effect = lambda seq: seq[0]
            response = self.post_idem("/api/v1/board/chance/now")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["dice_rolls_left"], 1)
        self.state.refresh_from_db()
        self.assertIsNone(self.state.next_dice_reset_at)

    def test_chance_now_requires_chance_cell(self):
        response = self.post_idem("/api/v1/board/chance/now")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "NOT_CHANCE_CELL")

    def test_chance_now_rejects_second_draw_at_same_cell(self):
        self.set_position(7)
        self.post_idem("/api/v1/board/chance/now", key="draw-1")

        self.set_position(7)
        response = self.post_idem("/api/v1/board/chance/now", key="draw-2")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "NOT_CHANCE_CELL")

    def test_chance_now_draws_second_card_and_flags_awaiting_discard(self):
        self.draw_card("card_reroll", source_cell_index=7)
        self.set_position(30, consumed=True)

        with patch("apps.board.services.random.choice") as choice_mock:
            choice_mock.side_effect = lambda seq: next(c for c in seq if c.card_id == "card_extra_roll")
            response = self.post_idem("/api/v1/board/chance/now")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["awaiting_discard"])
        self.assertEqual(
            TeamChanceCard.objects.filter(
                team=self.team, used_at__isnull=True, discarded_at__isnull=True
            ).count(),
            2,
        )

    # ---------------------------------------------------------------- chance/use

    def test_chance_use_grant_extra_roll(self):
        self.draw_card("card_extra_roll")
        response = self.post_idem("/api/v1/board/chance/use", {"card_id": "card_extra_roll"})

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["used"])
        self.assertEqual(data["dice_rolls_left"], 2)

    def test_chance_use_grant_extra_roll_clears_stale_dice_reset_timer(self):
        # 회귀 테스트: 주사위가 0개일 때 걸린 15분 회복 타이머가 이 카드로 먼저 채워진 뒤에도
        # 그대로 남아 있으면, 나중에 타이머가 만료될 때 회복 로직이 이미 채워진 주사위 위에
        # 1회를 더 얹어준다 (이중 지급). 카드 사용 시점에 타이머를 같이 지워야 한다.
        self.state.dice_rolls_left = 0
        self.state.next_dice_reset_at = timezone.now() + timedelta(minutes=15)
        self.state.save(update_fields=["dice_rolls_left", "next_dice_reset_at"])
        self.draw_card("card_extra_roll")

        response = self.post_idem("/api/v1/board/chance/use", {"card_id": "card_extra_roll"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["dice_rolls_left"], 1)

        self.state.refresh_from_db()
        self.assertIsNone(self.state.next_dice_reset_at)

    def test_chance_use_free_move(self):
        self.draw_card("card_free_travel")
        response = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_free_travel", "destination_index": 10}
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["to_index"], 10)
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 10)
        self.assertTrue(TeamCellConsumption.objects.filter(team=self.team, cell_id=10).exists())

    def test_chance_use_blocked_while_awaiting_discard(self):
        self.draw_card("card_reroll", source_cell_index=7)
        self.draw_card("card_free_travel", source_cell_index=30)

        response = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_free_travel", "destination_index": 10}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "CHANCE_CARD_AWAITING_DISCARD")

    def test_chance_use_reroll_finalizes_new_position(self):
        self.draw_card("card_reroll")
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            self.post_idem("/api/v1/board/dice/roll", key="roll-1")

        with patch("apps.board.services.random.randint", side_effect=[3, 3]):
            response = self.post_idem(
                "/api/v1/board/chance/use", {"card_id": "card_reroll"}, key="use-1"
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["dice_a"], 3)
        self.assertEqual(data["dice_b"], 3)
        self.assertEqual(data["rolled_number"], 6)
        self.assertEqual(data["to_index"], 7)  # 1 + 6
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 7)
        self.assertFalse(PendingDiceRoll.objects.filter(team=self.team).exists())

    def test_dice_roll_with_roll_twice_choose_card_finalizes_normally(self):
        self.draw_card("card_roll_twice_choose")

        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            response = self.post_idem("/api/v1/board/dice/roll")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["pending_confirm"])
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 3)

    def test_chance_use_move_offset_extends_pending_landing(self):
        self.draw_card("card_move_offset")
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            roll_response = self.post_idem("/api/v1/board/dice/roll", key="roll-1")  # candidate = 3

        roll_data = roll_response.json()["data"]
        self.assertEqual(roll_data["current_position"], 3)
        self.assertEqual(roll_data["skipped_cells"], [])
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 1)
        self.assertFalse(TeamCellConsumption.objects.filter(team=self.team, cell_id=3).exists())

        challenge = Challenge.objects.filter(difficulty=Cell.objects.get(cell_index=3).difficulty).first()
        open_response = self.post_idem(
            "/api/v1/board/cell/open", {"challenge_id": str(challenge.challenge_id)}, key="open-pending-offset"
        )
        self.assertEqual(open_response.status_code, 409)
        self.assertEqual(open_response.json()["code"], "PENDING_CONFIRM")

        response = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_move_offset", "offset": 2}, key="use-1"
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["from_index"], 3)
        self.assertEqual(data["to_index"], 5)
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 5)
        self.assertFalse(TeamCellConsumption.objects.filter(team=self.team, cell_id=3).exists())
        self.assertTrue(TeamCellConsumption.objects.filter(team=self.team, cell_id=5).exists())
        self.assertFalse(PendingDiceRoll.objects.filter(team=self.team).exists())

    def test_pending_confirm_allows_reroll_but_blocks_pre_roll_card(self):
        self.draw_card("card_reroll")
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            roll_response = self.post_idem("/api/v1/board/dice/roll", key="reroll-roll")
        self.assertEqual(roll_response.json()["code"], "SUCCESS")

        with patch("apps.board.services.random.randint", side_effect=[2, 2]):
            reroll_response = self.post_idem(
                "/api/v1/board/chance/use", {"card_id": "card_reroll"}, key="reroll-use"
            )
        self.assertEqual(reroll_response.json()["code"], "SUCCESS")
        self.assertFalse(PendingDiceRoll.objects.filter(team=self.team).exists())

        self.reset_card_scenario()
        self.draw_card("card_extra_roll")
        PendingDiceRoll.objects.create(
            team=self.team,
            dice_a=1,
            dice_b=1,
            rolled_number=2,
            previous_position=1,
            candidate_position=3,
            movement_path=[2, 3],
            board_event_code="CHALLENGE",
        )
        response = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_extra_roll"}, key="extra-pending"
        )
        self.assertEqual(set(response.json()), {"code", "message", "data"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "CHANCE_CARD_WRONG_TIMING")
        self.assertIsNone(TeamChanceCard.objects.get(team=self.team).used_at)

    def test_confirm_unlocks_problem_selection_after_pending_roll(self):
        self.draw_card("card_reroll")
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            roll_response = self.post_idem("/api/v1/board/dice/roll", key="pending-roll")
        self.assertTrue(roll_response.json()["data"]["pending_confirm"])

        challenge = Challenge.objects.filter(
            difficulty=Cell.objects.get(cell_index=3).difficulty
        ).first()
        blocked = self.post_idem(
            "/api/v1/board/cell/open", {"challenge_id": str(challenge.challenge_id)}, key="blocked-open"
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["code"], "PENDING_CONFIRM")

        confirm = self.post_idem("/api/v1/board/dice/confirm", key="pending-confirm")
        self.assertEqual(confirm.json()["code"], "SUCCESS")
        current = self.client.get("/api/v1/board/cell/current")
        self.assertEqual(current.json()["code"], "SUCCESS")
        self.assertEqual(current.json()["data"]["cell_index"], 3)
        challenge_id = current.json()["data"]["challenge_candidates"][0]["challenge_id"]
        opened = self.post_idem(
            "/api/v1/board/cell/open", {"challenge_id": challenge_id}, key="unblocked-open"
        )
        self.assertEqual(opened.json()["code"], "SUCCESS")
        self.assertTrue(
            TeamChallengeAccess.objects.filter(team=self.team, challenge_id=challenge_id).exists()
        )

    def test_all_seven_cards_complete_their_api_flow_once(self):
        for card_id in (
            "card_reroll",
            "card_roll_twice_choose",
            "card_move_offset",
            "card_free_travel",
            "card_extra_roll",
            "card_quarantine_defense",
            "card_move_to_quarantine",
        ):
            with self.subTest(card_id=card_id):
                self.reset_card_scenario()
                draw = self.draw_card(card_id)

                if card_id == "card_reroll":
                    with patch("apps.board.services.random.randint", side_effect=[1, 1]):
                        self.post_idem("/api/v1/board/dice/roll", key=f"{card_id}-roll")
                    with patch("apps.board.services.random.randint", side_effect=[2, 2]):
                        response = self.post_idem(
                            "/api/v1/board/chance/use", {"card_id": card_id}, key=f"{card_id}-use"
                        )
                elif card_id == "card_roll_twice_choose":
                    with patch("apps.board.services.random.randint", side_effect=[1, 1, 2, 2]):
                        response = self.post_idem(
                            "/api/v1/board/chance/use", {"card_id": card_id}, key=f"{card_id}-use"
                        )
                    self.assertEqual(response.json()["data"]["awaiting_confirm"], True)
                    response = self.post_idem(
                        "/api/v1/board/chance/confirm", {"choice": "FIRST"}, key=f"{card_id}-confirm"
                    )
                elif card_id == "card_move_offset":
                    with patch("apps.board.services.random.randint", side_effect=[1, 1]):
                        self.post_idem("/api/v1/board/dice/roll", key=f"{card_id}-roll")
                    response = self.post_idem(
                        "/api/v1/board/chance/use", {"card_id": card_id, "offset": 1}, key=f"{card_id}-use"
                    )
                elif card_id == "card_free_travel":
                    response = self.post_idem(
                        "/api/v1/board/chance/use",
                        {"card_id": card_id, "destination_index": 10},
                        key=f"{card_id}-use",
                    )
                elif card_id == "card_extra_roll":
                    self.state.dice_rolls_left = 0
                    self.state.save(update_fields=["dice_rolls_left"])
                    response = self.post_idem(
                        "/api/v1/board/chance/use", {"card_id": card_id}, key=f"{card_id}-use"
                    )
                elif card_id == "card_quarantine_defense":
                    self.state.is_quarantined = True
                    self.state.save(update_fields=["is_quarantined"])
                    response = self.post_idem(
                        "/api/v1/board/chance/use", {"card_id": card_id}, key=f"{card_id}-use"
                    )
                else:
                    response = self.post_idem(
                        "/api/v1/board/chance/use", {"card_id": card_id}, key=f"{card_id}-use"
                    )

                self.assertEqual(set(response.json()), {"code", "message", "data"})
                self.assertEqual(response.json()["code"], "SUCCESS")
                draw.refresh_from_db()
                self.assertIsNotNone(draw.used_at)
                second = self.post_idem(
                    "/api/v1/board/chance/use", {"card_id": card_id}, key=f"{card_id}-reuse"
                )
                self.assertEqual(second.status_code, 409)
                self.assertEqual(second.json()["code"], "CHANCE_CARD_ALREADY_USED")

    def test_chance_card_is_limited_to_owner_and_team_leader(self):
        draw = self.draw_card("card_extra_roll")
        self.as_other_leader()
        other_response = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_extra_roll"}, key="other-team-card"
        )
        self.assertEqual(other_response.status_code, 404)
        self.assertEqual(other_response.json()["code"], "CHANCE_CARD_NOT_FOUND")

        self.as_member()
        member_response = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_extra_roll"}, key="member-card"
        )
        self.assertEqual(member_response.status_code, 403)
        self.assertEqual(member_response.json()["code"], "NOT_TEAM_LEADER")

        self.as_leader()
        draw.refresh_from_db()
        self.assertIsNone(draw.used_at)

    def test_chance_card_duplicate_request_does_not_double_apply(self):
        draw = self.draw_card("card_extra_roll")
        first = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_extra_roll"}, key="same-card-request"
        )
        second = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_extra_roll"}, key="same-card-request"
        )
        self.assertEqual(first.json(), second.json())
        self.state.refresh_from_db()
        self.assertEqual(self.state.dice_rolls_left, 2)
        draw.refresh_from_db()
        self.assertIsNotNone(draw.used_at)

    def test_chance_use_quarantine_defense_escapes_immediately(self):
        self.state.is_quarantined = True
        self.state.quarantine_released_at = timezone.now() + timedelta(minutes=15)
        self.state.save(update_fields=["is_quarantined", "quarantine_released_at"])
        self.draw_card("card_quarantine_defense")

        response = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_quarantine_defense"}
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["is_quarantined"])
        self.assertEqual(data["dice_rolls_left"], 2)
        self.state.refresh_from_db()
        self.assertFalse(self.state.is_quarantined)
        self.assertEqual(self.state.dice_rolls_left, 2)

    def test_chance_use_quarantine_defense_keeps_quarantine_consumed_for_next_skip(self):
        start_cell = self.set_position(14)
        self.mark_challenge_solved(start_cell)
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            enter_response = self.post_idem("/api/v1/board/dice/roll", key="enter-quarantine")
        self.assertEqual(enter_response.status_code, 200)
        self.state.refresh_from_db()
        self.assertTrue(self.state.is_quarantined)
        self.assertTrue(TeamCellConsumption.objects.filter(team=self.team, cell_id=16).exists())

        self.draw_card("card_quarantine_defense")
        escape_response = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_quarantine_defense"}, key="escape-card"
        )
        self.assertEqual(escape_response.status_code, 200)

        self.state.position_id = 14
        self.state.dice_rolls_left = 1
        self.state.save(update_fields=["position", "dice_rolls_left"])
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            response = self.post_idem("/api/v1/board/dice/roll", key="roll-after-card-escape")

        data = response.json()["data"]
        self.assertEqual(data["current_position"], 17)
        self.assertEqual(data["skipped_cells"], [16])
        self.state.refresh_from_db()
        self.assertFalse(self.state.is_quarantined)

    def test_chance_use_move_to_consumed_quarantine_does_not_lock_again(self):
        quarantine_cell = Cell.objects.get(type=Cell.CellType.QUARANTINE)
        TeamCellConsumption.objects.create(team=self.team, cell=quarantine_cell)
        self.draw_card("card_move_to_quarantine")

        response = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_move_to_quarantine"}
        )

        self.assertEqual(response.status_code, 200)
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, quarantine_cell.cell_index)
        self.assertFalse(self.state.is_quarantined)

    def test_chance_use_unknown_card_not_found(self):
        response = self.post_idem("/api/v1/board/chance/use", {"card_id": "card_reroll"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "CHANCE_CARD_NOT_FOUND")

    def test_chance_use_requires_card_id(self):
        response = self.post_idem("/api/v1/board/chance/use", {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "CARD_ID_REQUIRED")

    # ---------------------------------------------------------------- chance/confirm

    def test_chance_confirm_roll_twice_choose(self):
        self.draw_card("card_roll_twice_choose")

        with patch("apps.board.services.random.randint", side_effect=[1, 1, 3, 3]):
            use_response = self.post_idem(
                "/api/v1/board/chance/use", {"card_id": "card_roll_twice_choose"}, key="use-1"
            )
        self.assertEqual(use_response.status_code, 200)
        use_data = use_response.json()["data"]
        self.assertTrue(use_data["awaiting_confirm"])
        self.assertEqual(use_data["first_number"], 2)
        self.assertEqual(use_data["second_number"], 6)
        draw = TeamChanceCard.objects.get(team=self.team, card_id="card_roll_twice_choose")
        self.assertEqual(draw.pending_first_number, 2)
        self.assertEqual(draw.pending_second_number, 6)
        self.assertTrue(PendingDiceRoll.objects.filter(team=self.team, rolled_number=2).exists())
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 1)
        self.assertEqual(self.state.dice_rolls_left, 0)

        response = self.post_idem(
            "/api/v1/board/chance/confirm", {"choice": "SECOND"}, key="confirm-1"
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["chosen_number"], 6)
        self.assertEqual(data["to_index"], 7)
        self.state.refresh_from_db()
        self.assertEqual(self.state.position_id, 7)
        self.assertFalse(PendingDiceRoll.objects.filter(team=self.team).exists())

    def test_dice_confirm_blocked_while_roll_twice_choose_awaits_choice(self):
        self.draw_card("card_roll_twice_choose")
        with patch("apps.board.services.random.randint", side_effect=[1, 1, 3, 3]):
            self.post_idem(
                "/api/v1/board/chance/use", {"card_id": "card_roll_twice_choose"}, key="use-1"
            )

        response = self.post_idem("/api/v1/board/dice/confirm", key="dice-confirm")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "CHANCE_CONFIRM_NOT_FOUND")

    def test_chance_confirm_without_pending_choice_fails(self):
        response = self.post_idem("/api/v1/board/chance/confirm", {"choice": "FIRST"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "CHANCE_CONFIRM_NOT_FOUND")

    # ---------------------------------------------------------------- chance/discard

    def test_chance_discard_leaves_the_other_card_usable(self):
        first = self.draw_card("card_extra_roll", source_cell_index=7)
        self.draw_card("card_free_travel", source_cell_index=30)

        response = self.post_idem(
            "/api/v1/board/chance/discard", {"card_id": "card_free_travel"}, key="discard-1"
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["discarded_card_id"], "card_free_travel")
        self.assertEqual(data["kept_card_id"], "card_extra_roll")

        first.refresh_from_db()
        self.assertIsNone(first.used_at)
        self.assertIsNone(first.discarded_at)
        discarded = TeamChanceCard.objects.get(card_id="card_free_travel")
        self.assertIsNotNone(discarded.discarded_at)

        # 1장만 남았으니 이제 정상적으로 사용할 수 있다
        use_response = self.post_idem(
            "/api/v1/board/chance/use", {"card_id": "card_extra_roll"}, key="use-1"
        )
        self.assertEqual(use_response.status_code, 200)

    def test_chance_discard_requires_two_held_cards(self):
        self.draw_card("card_extra_roll")

        response = self.post_idem("/api/v1/board/chance/discard", {"card_id": "card_extra_roll"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "NO_CARD_TO_DISCARD")

    def test_chance_discard_rejects_unknown_card_id(self):
        self.draw_card("card_reroll", source_cell_index=7)
        self.draw_card("card_free_travel", source_cell_index=30)

        response = self.post_idem("/api/v1/board/chance/discard", {"card_id": "card_move_offset"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "CHANCE_CARD_NOT_FOUND")

    def test_chance_discard_forbidden_for_non_leader(self):
        self.draw_card("card_reroll", source_cell_index=7)
        self.draw_card("card_free_travel", source_cell_index=30)
        self.as_member()

        response = self.post_idem("/api/v1/board/chance/discard", {"card_id": "card_free_travel"})

        self.assertEqual(response.status_code, 403)

    # ---------------------------------------------------------------- roulette/spin

    def test_roulette_spin_grants_one_of_fixed_rewards(self):
        self.set_position(25, consumed=True)
        self.team.mileage = 10
        self.team.save(update_fields=["mileage"])

        with patch("apps.board.services.random.choice", return_value=150):
            response = self.post_idem("/api/v1/board/roulette/spin")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["roulette_result"], {"label": "마일리지 150"})
        self.assertEqual(data["mileage_gained"], 150)
        self.assertEqual(data["total_mileage"], 160)

        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 160)
        self.assertTrue(TeamCellConsumption.objects.filter(team=self.team, cell_id=25).exists())
        self.assertTrue(
            MileageHistory.objects.filter(
                team=self.team,
                type="ROULETTE",
                amount=150,
                reason="ROULETTE_CELL:25",
            ).exists()
        )

    def test_roulette_spin_replays_response_for_same_idempotency_key(self):
        self.set_position(25, consumed=True)

        with patch("apps.board.services.random.choice", return_value=50):
            first = self.post_idem("/api/v1/board/roulette/spin", key="roulette-1")
        second = self.post_idem("/api/v1/board/roulette/spin", key="roulette-1")

        self.assertEqual(first.json(), second.json())
        self.assertEqual(
            MileageHistory.objects.filter(team=self.team, type="ROULETTE").count(),
            1,
        )

    def test_roulette_spin_cannot_be_used_twice_with_new_key(self):
        self.set_position(25, consumed=True)

        with patch("apps.board.services.random.choice", return_value=50):
            self.post_idem("/api/v1/board/roulette/spin", key="roulette-1")
        response = self.post_idem("/api/v1/board/roulette/spin", key="roulette-2")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "ROULETTE_ALREADY_SPUN")
        self.assertEqual(
            MileageHistory.objects.filter(team=self.team, type="ROULETTE").count(),
            1,
        )

    def test_roulette_spin_requires_roulette_cell(self):
        response = self.post_idem("/api/v1/board/roulette/spin")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "NOT_ROULETTE_CELL")

    def test_roulette_spin_rejects_request_body(self):
        self.set_position(25, consumed=True)

        response = self.post_idem("/api/v1/board/roulette/spin", {"value": 50})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "REQUEST_BODY_NOT_ALLOWED")

    def test_roulette_spin_forbidden_for_non_leader(self):
        self.set_position(25, consumed=True)
        self.as_member()

        response = self.post_idem("/api/v1/board/roulette/spin")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "NOT_TEAM_LEADER")

    # ---------------------------------------------------------------- quarantine/escape

    def enter_quarantine(self):
        self.state.is_quarantined = True
        self.state.quarantine_released_at = timezone.now() + timedelta(minutes=15)
        self.state.save(update_fields=["is_quarantined", "quarantine_released_at"])

    def test_quarantine_escape_with_valid_code_succeeds(self):
        self.enter_quarantine()
        QuarantineEscapeCode.objects.create(code="AAAAAAAA")

        response = self.post_idem("/api/v1/board/quarantine/escape", {"code": "AAAAAAAA"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["is_quarantined"])
        self.state.refresh_from_db()
        self.assertFalse(self.state.is_quarantined)
        escape_code = QuarantineEscapeCode.objects.get(code="AAAAAAAA")
        self.assertEqual(escape_code.used_by_team, self.team)
        self.assertIsNotNone(escape_code.used_at)

    def test_quarantine_escape_code_keeps_quarantine_consumed_for_next_skip(self):
        start_cell = self.set_position(14)
        self.mark_challenge_solved(start_cell)
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            enter_response = self.post_idem("/api/v1/board/dice/roll", key="enter-quarantine")
        self.assertEqual(enter_response.status_code, 200)
        self.state.refresh_from_db()
        self.assertTrue(self.state.is_quarantined)
        self.assertTrue(TeamCellConsumption.objects.filter(team=self.team, cell_id=16).exists())

        QuarantineEscapeCode.objects.create(code="BBBBBBBB")
        escape_response = self.post_idem(
            "/api/v1/board/quarantine/escape", {"code": "BBBBBBBB"}, key="escape-code"
        )
        self.assertEqual(escape_response.status_code, 200)

        self.state.position_id = 14
        self.state.dice_rolls_left = 1
        self.state.save(update_fields=["position", "dice_rolls_left"])
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            response = self.post_idem("/api/v1/board/dice/roll", key="roll-after-code-escape")

        data = response.json()["data"]
        self.assertEqual(data["current_position"], 17)
        self.assertEqual(data["skipped_cells"], [16])
        self.state.refresh_from_db()
        self.assertFalse(self.state.is_quarantined)

    def test_quarantine_escape_code_cannot_be_reused_by_another_team(self):
        self.enter_quarantine()
        QuarantineEscapeCode.objects.create(
            code="AAAAAAAA", used_by_team=self.other_team, used_at=timezone.now()
        )

        response = self.post_idem("/api/v1/board/quarantine/escape", {"code": "AAAAAAAA"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "QUARANTINE_CODE_ALREADY_USED")
        self.state.refresh_from_db()
        self.assertTrue(self.state.is_quarantined)

    def test_quarantine_escape_unknown_code_not_found(self):
        self.enter_quarantine()

        response = self.post_idem("/api/v1/board/quarantine/escape", {"code": "NOPE0000"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "QUARANTINE_CODE_INVALID")

    def test_quarantine_escape_requires_code(self):
        self.enter_quarantine()

        response = self.post_idem("/api/v1/board/quarantine/escape", {})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "QUARANTINE_CODE_REQUIRED")

    def test_quarantine_escape_requires_being_quarantined(self):
        QuarantineEscapeCode.objects.create(code="AAAAAAAA")

        response = self.post_idem("/api/v1/board/quarantine/escape", {"code": "AAAAAAAA"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "NOT_QUARANTINED")

    def test_quarantine_escape_forbidden_for_non_leader(self):
        self.enter_quarantine()
        QuarantineEscapeCode.objects.create(code="AAAAAAAA")
        self.as_member()

        response = self.post_idem("/api/v1/board/quarantine/escape", {"code": "AAAAAAAA"})

        self.assertEqual(response.status_code, 403)
