from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.board.models import (
    Cell,
    Challenge,
    DiceRoll,
    TeamBoardState,
    TeamCellCandidate,
    TeamChallengeAccess,
)
from apps.teams.models import DEFAULT_TEAM_NAME, Team


class SingleTeamBoardApiTests(TestCase):
    def setUp(self):
        call_command("seed_board", verbosity=0)

    def move_team_to_first_challenge_cell(self, difficulty=Cell.Difficulty.EASY):
        team = Team.objects.get(name=DEFAULT_TEAM_NAME)
        state = TeamBoardState.objects.get(team=team)
        target_cell = Cell.objects.filter(
            type=Cell.CellType.CHALLENGE,
            difficulty=difficulty,
        ).first()
        state.position = target_cell
        state.dice_rolls_left = 1
        state.active_challenge_access = None
        state.save(update_fields=["position", "dice_rolls_left", "active_challenge_access"])
        return team, state, target_cell

    def test_dashboard_page_loads(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MSG Board Preview")

    def test_board_returns_notion_shape(self):
        response = self.client.get("/api/v1/board")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], "SUCCESS")
        self.assertEqual(body["data"]["total_cell_count"], 36)
        self.assertEqual(len(body["data"]["cells"]), 36)
        self.assertEqual(body["data"]["cells"][0]["cell_index"], 1)
        special_cells = {
            cell["cell_index"]: (cell["type"], cell["name"])
            for cell in body["data"]["cells"]
            if cell["type"] != Cell.CellType.CHALLENGE
        }
        self.assertEqual(
            special_cells,
            {
                1: (Cell.CellType.START, "출발"),
                7: (Cell.CellType.CHANCE, "황금열쇠"),
                16: (Cell.CellType.QUARANTINE, "무인도"),
                21: (Cell.CellType.AIRPORT, "기차"),
                25: (Cell.CellType.ROULETTE, "룰렛"),
                30: (Cell.CellType.CHANCE, "황금열쇠"),
            },
        )

    def test_board_cell_exposes_difficulty_field(self):
        response = self.client.get("/api/v1/board")

        cells = response.json()["data"]["cells"]
        self.assertEqual(
            set(cells[0].keys()),
            {"cell_index", "type", "difficulty", "name"},
        )
        for cell in cells:
            if cell["type"] == Cell.CellType.CHALLENGE:
                self.assertIn(cell["difficulty"], Cell.Difficulty.values)
            else:
                self.assertIsNone(cell["difficulty"])

    def test_board_returns_load_failed_when_board_is_not_seeded(self):
        TeamBoardState.objects.all().delete()
        Cell.objects.all().delete()

        response = self.client.get("/api/v1/board")

        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["code"], "BOARD_LOAD_FAILED")
        self.assertEqual(body["message"], "보드 정보를 불러오지 못했습니다.")
        self.assertIsNone(body["data"])

    def test_team_me_returns_default_team_and_board_state(self):
        response = self.client.get("/api/v1/teams/me")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], "SUCCESS")
        self.assertEqual(body["data"]["team"]["name"], DEFAULT_TEAM_NAME)
        self.assertEqual(body["data"]["team"]["board_position"], 1)
        self.assertEqual(body["data"]["board_state"]["position"], 1)
        self.assertEqual(body["data"]["board_state"]["dice_rolls_left"], 1)

    def test_board_me_returns_current_board_state(self):
        response = self.client.get("/api/v1/board/me")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data"]["position"], 1)
        self.assertEqual(body["data"]["type"], Cell.CellType.START)
        self.assertIsNone(body["data"]["active_challenge"])
        self.assertEqual(
            set(body["data"].keys()),
            {
                "position",
                "type",
                "is_quarantined",
                "dice_rolls_left",
                "next_dice_reset_at",
                "quarantine_attempts_left",
                "airport_move_used",
                "has_passed_start",
                "chance_cards",
                "active_challenge",
            },
        )
        self.assertEqual(body["data"]["dice_rolls_left"], 1)
        self.assertIsNone(body["data"]["next_dice_reset_at"])
        self.assertEqual(body["data"]["quarantine_attempts_left"], 0)
        self.assertFalse(body["data"]["airport_move_used"])
        self.assertFalse(body["data"]["has_passed_start"])

    def test_dice_status_returns_notion_shape(self):
        response = self.client.get("/api/v1/board/dice/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], "SUCCESS")
        self.assertTrue(body["data"]["can_roll"])
        self.assertEqual(body["data"]["dice_rolls_left"], 1)
        self.assertFalse(body["data"]["timer_running"])
        self.assertIsNone(body["data"]["blocked_reason"])
        self.assertEqual(
            set(body["data"].keys()),
            {
                "can_roll",
                "dice_rolls_left",
                "is_quarantined",
                "timer_running",
                "blocked_reason",
                "server_time",
                "next_dice_reset_at",
                "quarantine_released_at",
            },
        )
        self.assertIsNone(body["data"]["next_dice_reset_at"])
        self.assertIsNone(body["data"]["quarantine_released_at"])

    def test_dice_roll_moves_team_without_opening_challenge(self):
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            response = self.client.post("/api/v1/board/dice/roll")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        data = body["data"]

        self.assertEqual(body["code"], "SUCCESS")
        self.assertEqual(data["dice_a"], 1)
        self.assertEqual(data["dice_b"], 1)
        self.assertEqual(data["rolled_number"], 2)
        self.assertEqual(data["previous_position"], 1)
        self.assertEqual(data["current_position"], 3)
        self.assertEqual(data["skipped_cells"], [])
        self.assertFalse(data["passed_start"])
        self.assertEqual(data["start_reward"]["mileage_gained"], 0)
        self.assertIn(data["board_event_code"], ["NONE", "CHALLENGE", "CHANCE", "AIRPORT", "QUARANTINE", "ROULETTE"])
        self.assertEqual(DiceRoll.objects.count(), 1)
        self.assertEqual(TeamChallengeAccess.objects.count(), 0)

    def test_dice_recharges_one_roll_after_reset_time_passes(self):
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            self.client.post("/api/v1/board/dice/roll")

        team = Team.objects.get(name=DEFAULT_TEAM_NAME)
        state = TeamBoardState.objects.get(team=team)
        self.assertEqual(state.dice_rolls_left, 0)
        self.assertIsNotNone(state.next_dice_reset_at)

        state.next_dice_reset_at = timezone.now() - timedelta(minutes=1)
        state.save(update_fields=["next_dice_reset_at"])

        response = self.client.get("/api/v1/board/dice/status")

        body = response.json()["data"]
        self.assertTrue(body["can_roll"])
        self.assertEqual(body["dice_rolls_left"], 1)
        self.assertIsNone(body["next_dice_reset_at"])

    def test_dice_recharge_never_grants_more_than_one_roll_no_matter_how_long_it_waited(self):
        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            self.client.post("/api/v1/board/dice/roll")

        team = Team.objects.get(name=DEFAULT_TEAM_NAME)
        state = TeamBoardState.objects.get(team=team)
        state.next_dice_reset_at = timezone.now() - timedelta(minutes=30)
        state.save(update_fields=["next_dice_reset_at"])

        self.client.get("/api/v1/board/dice/status")
        second_response = self.client.get("/api/v1/board/dice/status")

        self.assertEqual(second_response.json()["data"]["dice_rolls_left"], 1)

    def test_landing_on_quarantine_cell_locks_dice_for_fifteen_minutes(self):
        team = Team.objects.get(name=DEFAULT_TEAM_NAME)
        state = TeamBoardState.objects.get(team=team)
        state.position = Cell.objects.get(cell_index=14)
        state.save(update_fields=["position"])

        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            roll_response = self.client.post("/api/v1/board/dice/roll")

        self.assertEqual(roll_response.json()["data"]["current_position"], 16)
        state.refresh_from_db()
        self.assertTrue(state.is_quarantined)
        self.assertIsNotNone(state.quarantine_released_at)

        status_response = self.client.get("/api/v1/board/dice/status")
        status_data = status_response.json()["data"]
        self.assertFalse(status_data["can_roll"])
        self.assertEqual(status_data["blocked_reason"], "QUARANTINED")
        self.assertTrue(status_data["is_quarantined"])

        second_roll_response = self.client.post("/api/v1/board/dice/roll")
        self.assertEqual(second_roll_response.status_code, 409)
        self.assertEqual(second_roll_response.json()["code"], "QUARANTINED")

    def test_quarantine_auto_releases_once_lock_time_passes(self):
        team = Team.objects.get(name=DEFAULT_TEAM_NAME)
        state = TeamBoardState.objects.get(team=team)
        state.position = Cell.objects.get(cell_index=14)
        state.save(update_fields=["position"])

        with patch("apps.board.services.random.randint", side_effect=[1, 1]):
            self.client.post("/api/v1/board/dice/roll")

        state.refresh_from_db()
        state.quarantine_released_at = timezone.now() - timedelta(minutes=1)
        state.save(update_fields=["quarantine_released_at"])

        response = self.client.get("/api/v1/board/dice/status")
        data = response.json()["data"]
        self.assertFalse(data["is_quarantined"])
        self.assertIsNone(data["quarantine_released_at"])
        self.assertNotEqual(data["blocked_reason"], "QUARANTINED")

    def test_dice_roll_skips_visited_challenge_cells(self):
        team = Team.objects.get(name=DEFAULT_TEAM_NAME)
        state = TeamBoardState.objects.get(team=team)
        visited_cell = Cell.objects.get(cell_index=2)
        challenge = Challenge.objects.filter(difficulty=visited_cell.difficulty).first()
        TeamChallengeAccess.objects.create(
            team=team,
            challenge=challenge,
            source_cell=visited_cell,
            status=TeamChallengeAccess.Status.CLEARED,
        )
        state.position_id = 35
        state.dice_rolls_left = 1
        state.active_challenge_access = None
        state.save(update_fields=["position", "dice_rolls_left", "active_challenge_access"])

        with patch("apps.board.services.random.randint", side_effect=[1, 2]):
            response = self.client.post("/api/v1/board/dice/roll")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["rolled_number"], 3)
        self.assertEqual(data["previous_position"], 35)
        self.assertEqual(data["skipped_cells"], [2])
        self.assertTrue(data["passed_start"])
        self.assertEqual(data["start_reward"]["mileage_gained"], 50)
        self.assertEqual(data["current_position"], 3)
        team.refresh_from_db()
        self.assertEqual(team.mileage_balance, 50)

    def test_second_dice_roll_is_blocked(self):
        self.client.post("/api/v1/board/dice/roll")
        response = self.client.post("/api/v1/board/dice/roll")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "NO_ROLL_LEFT")

    def test_dice_reset_allows_another_roll(self):
        self.client.post("/api/v1/board/dice/roll")
        reset_response = self.client.post("/api/v1/board/dice/reset")

        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(reset_response.json()["data"]["dice_rolls_left"], 1)

        roll_response = self.client.post("/api/v1/board/dice/roll")

        self.assertEqual(roll_response.status_code, 200)
        self.assertEqual(DiceRoll.objects.count(), 2)

    def test_airport_move_returns_notion_shape(self):
        team = Team.objects.get(name=DEFAULT_TEAM_NAME)
        state = TeamBoardState.objects.get(team=team)
        state.position = Cell.objects.get(cell_index=21)
        state.save(update_fields=["position"])

        response = self.client.post(
            "/api/v1/board/airport/move",
            {"destination_index": 5},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], "SUCCESS")
        self.assertEqual(
            set(body["data"].keys()),
            {"previous_position", "current_position", "board_event_code", "passed_start", "start_reward"},
        )
        self.assertEqual(body["data"]["previous_position"], 21)
        self.assertEqual(body["data"]["current_position"], 5)
        self.assertFalse(body["data"]["passed_start"])
        self.assertEqual(body["data"]["start_reward"], {"mileage_gained": 0, "roll_gained": 0})

    def test_airport_move_to_start_grants_start_bonus(self):
        team = Team.objects.get(name=DEFAULT_TEAM_NAME)
        state = TeamBoardState.objects.get(team=team)
        state.position = Cell.objects.get(cell_index=21)
        state.save(update_fields=["position"])

        response = self.client.post(
            "/api/v1/board/airport/move",
            {"destination_index": 1},
            content_type="application/json",
        )

        data = response.json()["data"]
        self.assertTrue(data["passed_start"])
        self.assertEqual(data["start_reward"]["mileage_gained"], 50)
        team.refresh_from_db()
        self.assertEqual(team.mileage_balance, 50)

    def test_seed_creates_thirty_demo_challenges_by_difficulty(self):
        self.assertEqual(Challenge.objects.count(), 30)
        self.assertEqual(Challenge.objects.filter(difficulty=Cell.Difficulty.EASY).count(), 12)
        self.assertEqual(Challenge.objects.filter(difficulty=Cell.Difficulty.MEDIUM).count(), 12)
        self.assertEqual(Challenge.objects.filter(difficulty=Cell.Difficulty.HARD).count(), 6)

    def test_challenge_list_returns_all_thirty_with_progress(self):
        response = self.client.get("/api/v1/board/challenges")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total_count"], 30)
        self.assertEqual(data["opened_count"], 0)
        self.assertEqual(data["solved_count"], 0)
        first_challenge = Challenge.objects.order_by("challenge_number").first()
        self.assertEqual(data["challenges"][0]["challenge_id"], str(first_challenge.id))
        self.assertFalse(data["challenges"][0]["is_opened"])
        self.assertFalse(data["challenges"][0]["is_solved"])

    def test_cell_current_returns_three_random_candidates_for_challenge_cell(self):
        self.move_team_to_first_challenge_cell(Cell.Difficulty.EASY)

        response = self.client.get("/api/v1/board/cell/current")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        candidates = body["data"]["challenge_candidates"]
        self.assertEqual(body["data"]["type"], Cell.CellType.CHALLENGE)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(TeamCellCandidate.objects.count(), 3)

    def test_cell_current_returns_notion_shape_for_challenge_cell(self):
        self.move_team_to_first_challenge_cell(Cell.Difficulty.EASY)

        response = self.client.get("/api/v1/board/cell/current")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], "SUCCESS")
        self.assertEqual(set(body["data"].keys()), {"cell_index", "type", "challenge_candidates"})
        candidate = body["data"]["challenge_candidates"][0]
        self.assertEqual(set(candidate.keys()), {"challenge_id", "title", "category", "score"})

    def test_cell_current_returns_empty_candidates_for_non_challenge_cell(self):
        response = self.client.get("/api/v1/board/cell/current")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data"]["cell_index"], 1)
        self.assertEqual(body["data"]["type"], Cell.CellType.START)
        self.assertEqual(body["data"]["challenge_candidates"], [])

    def test_cell_open_opens_selected_candidate(self):
        team, _, target_cell = self.move_team_to_first_challenge_cell(Cell.Difficulty.EASY)
        current_response = self.client.get("/api/v1/board/cell/current")
        challenge_id = current_response.json()["data"]["challenge_candidates"][0]["challenge_id"]

        open_response = self.client.post(
            "/api/v1/board/cell/open",
            {"challenge_id": challenge_id},
            content_type="application/json",
        )

        self.assertEqual(open_response.status_code, 200)
        body = open_response.json()
        self.assertEqual(body["data"]["challenge_id"], challenge_id)
        self.assertEqual(body["data"]["cell_index"], target_cell.cell_index)
        self.assertEqual(TeamChallengeAccess.objects.filter(team=team).count(), 1)

        state = TeamBoardState.objects.get(team=team)
        self.assertIsNotNone(state.active_challenge_access)

    def test_opened_challenge_is_not_reused_in_next_same_difficulty_candidates(self):
        team, state, first_cell = self.move_team_to_first_challenge_cell(Cell.Difficulty.EASY)
        current_response = self.client.get("/api/v1/board/cell/current")
        opened_challenge_id = current_response.json()["data"]["challenge_candidates"][0]["challenge_id"]
        self.client.post(
            "/api/v1/board/cell/open",
            {"challenge_id": opened_challenge_id},
            content_type="application/json",
        )

        next_cell = (
            Cell.objects.filter(type=Cell.CellType.CHALLENGE, difficulty=Cell.Difficulty.EASY)
            .exclude(cell_index=first_cell.cell_index)
            .first()
        )
        state.refresh_from_db()
        state.position = next_cell
        state.active_challenge_access = None
        state.save(update_fields=["position", "active_challenge_access"])

        next_response = self.client.get("/api/v1/board/cell/current")
        next_candidate_ids = {
            candidate["challenge_id"]
            for candidate in next_response.json()["data"]["challenge_candidates"]
        }

        self.assertNotIn(opened_challenge_id, next_candidate_ids)
        self.assertEqual(TeamChallengeAccess.objects.filter(team=team).count(), 1)

    def test_same_cell_cannot_open_another_challenge_after_solve(self):
        self.move_team_to_first_challenge_cell(Cell.Difficulty.HARD)
        current_response = self.client.get("/api/v1/board/cell/current")
        candidates = current_response.json()["data"]["challenge_candidates"]
        first_challenge_id = candidates[0]["challenge_id"]
        second_challenge_id = candidates[1]["challenge_id"]

        self.client.post(
            "/api/v1/board/cell/open",
            {"challenge_id": first_challenge_id},
            content_type="application/json",
        )
        self.client.post("/api/v1/board/challenge/solve")

        current_after_solve = self.client.get("/api/v1/board/cell/current")
        self.assertEqual(current_after_solve.status_code, 200)
        self.assertEqual(current_after_solve.json()["data"]["challenge_candidates"], [])

        second_open_response = self.client.post(
            "/api/v1/board/cell/open",
            {"challenge_id": second_challenge_id},
            content_type="application/json",
        )

        self.assertEqual(second_open_response.status_code, 409)
        self.assertEqual(second_open_response.json()["code"], "CELL_ALREADY_OPENED")
        self.assertEqual(TeamChallengeAccess.objects.count(), 1)

    def test_opened_challenges_returns_solve_summary(self):
        team, _, _ = self.move_team_to_first_challenge_cell(Cell.Difficulty.EASY)
        current_response = self.client.get("/api/v1/board/cell/current")
        challenge_id = current_response.json()["data"]["challenge_candidates"][0]["challenge_id"]
        self.client.post(
            "/api/v1/board/cell/open",
            {"challenge_id": challenge_id},
            content_type="application/json",
        )
        self.client.post("/api/v1/board/challenge/solve")

        response = self.client.get("/api/v1/board/opened_challenges")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total_count"], 1)
        self.assertEqual(data["solved_count"], 1)
        self.assertEqual(data["opened_challenges"][0]["challenge_id"], challenge_id)
        self.assertTrue(data["opened_challenges"][0]["is_solved"])
        self.assertGreater(data["total_score"], 0)
        self.assertEqual(TeamChallengeAccess.objects.filter(team=team).count(), 1)

        challenges_response = self.client.get("/api/v1/board/challenges")
        challenge_item = next(
            challenge
            for challenge in challenges_response.json()["data"]["challenges"]
            if challenge["challenge_id"] == challenge_id
        )
        self.assertTrue(challenge_item["is_opened"])
        self.assertTrue(challenge_item["is_solved"])
        self.assertIsNotNone(challenge_item["cell_index"])

    def test_solving_active_challenge_grants_dice_immediately(self):
        team, state, _ = self.move_team_to_first_challenge_cell(Cell.Difficulty.HARD)
        current_response = self.client.get("/api/v1/board/cell/current")
        challenge_id = current_response.json()["data"]["challenge_candidates"][0]["challenge_id"]
        self.client.post(
            "/api/v1/board/cell/open",
            {"challenge_id": challenge_id},
            content_type="application/json",
        )
        state.dice_rolls_left = 0
        state.save(update_fields=["dice_rolls_left"])

        solve_response = self.client.post("/api/v1/board/challenge/solve")

        self.assertEqual(solve_response.status_code, 200)
        solve_data = solve_response.json()["data"]
        self.assertTrue(solve_data["is_extra_dice_granted"])
        self.assertEqual(solve_data["dice_rolls_left"], 1)
        state.refresh_from_db()
        self.assertEqual(state.dice_rolls_left, 1)
        self.assertEqual(
            TeamChallengeAccess.objects.filter(team=team).count(),
            1,
        )
        status_response = self.client.get("/api/v1/board/dice/status")
        self.assertTrue(status_response.json()["data"]["can_roll"])

    def test_timer_running_blocks_dice_roll_until_solved(self):
        self.move_team_to_first_challenge_cell(Cell.Difficulty.EASY)
        current_response = self.client.get("/api/v1/board/cell/current")
        challenge_id = current_response.json()["data"]["challenge_candidates"][0]["challenge_id"]
        self.client.post(
            "/api/v1/board/cell/open",
            {"challenge_id": challenge_id},
            content_type="application/json",
        )

        blocked_response = self.client.post("/api/v1/board/dice/roll")
        self.assertEqual(blocked_response.status_code, 409)
        self.assertEqual(blocked_response.json()["code"], "TIMER_RUNNING")

        self.client.post("/api/v1/board/challenge/solve")
        roll_response = self.client.post("/api/v1/board/dice/roll")
        self.assertEqual(roll_response.status_code, 200)
