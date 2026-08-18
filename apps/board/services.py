import random
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.board.models import (
    Cell,
    Challenge,
    DiceRoll,
    TeamBoardState,
    TeamCellCandidate,
    TeamChallengeAccess,
)
from apps.teams.models import Team, get_default_team


BOARD_SIZE = 36
FIRST_CELL_INDEX = 1
START_CELL_INDEX = 1
CHALLENGE_CANDIDATE_COUNT = 3
SOLVE_LIMIT_SECONDS = 900
START_PASS_MILEAGE_REWARD = 50
DICE_RECHARGE_INTERVAL = timedelta(minutes=15)
QUARANTINE_LOCK_INTERVAL = timedelta(minutes=15)


class BoardNotReadyError(Exception):
    pass


class NoDiceRollsLeftError(Exception):
    pass


class TimerRunningError(Exception):
    pass


class NotChallengeCellError(Exception):
    pass


class ChallengeNotCandidateError(Exception):
    pass


class CellAlreadyOpenedError(Exception):
    pass


class InvalidDestinationIndexError(Exception):
    pass


class NotAirportCellError(Exception):
    pass


class AirportMoveAlreadyUsedError(Exception):
    pass


class QuarantinedError(Exception):
    pass


def apply_pending_dice_recharge(state):
    """Grant exactly one dice roll if the 15-minute recharge timer has elapsed.

    The recharge is a one-time top-up tied to the last roll, not a repeating
    timer: however long the team waits past `next_dice_reset_at`, it only
    ever adds 1 (never 2+ for waiting 30+ minutes).
    """
    if state.next_dice_reset_at is None or timezone.now() < state.next_dice_reset_at:
        return state

    state.dice_rolls_left += 1
    state.next_dice_reset_at = None
    state.save(update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"])
    return state


def apply_pending_quarantine_release(state):
    """Auto-lift quarantine once the 15-minute lock has elapsed."""
    if not state.is_quarantined or state.quarantine_released_at is None:
        return state
    if timezone.now() < state.quarantine_released_at:
        return state

    state.is_quarantined = False
    state.quarantine_released_at = None
    state.save(update_fields=["is_quarantined", "quarantine_released_at", "updated_at"])
    return state


def enter_quarantine_if_landed(state, cell):
    if cell.type != Cell.CellType.QUARANTINE:
        return
    state.is_quarantined = True
    state.quarantine_released_at = timezone.now() + QUARANTINE_LOCK_INTERVAL


def get_or_create_default_team_state():
    team = get_default_team()
    start_cell = Cell.objects.filter(cell_index=START_CELL_INDEX).first()
    if start_cell is None:
        return team, None

    state, _ = TeamBoardState.objects.get_or_create(
        team=team,
        defaults={
            "position": start_cell,
            "dice_rolls_left": 1,
        },
    )
    if team.board_position != state.position_id:
        Team.objects.filter(pk=team.pk).update(board_position=state.position_id)
        team.board_position = state.position_id

    state = apply_pending_dice_recharge(state)
    state = apply_pending_quarantine_release(state)

    return team, state


def get_event_for_cell(cell):
    if cell.type == Cell.CellType.START:
        return "NONE"
    return cell.type


def calculate_position_with_skipped_cells(team, previous_position, rolled_number):
    visited_cell_indexes = set(
        TeamChallengeAccess.objects.filter(team=team).values_list(
            "source_cell_id",
            flat=True,
        )
    )
    skipped_cells = []
    passed_start = False
    counted_steps = 0
    cursor = previous_position
    guard = 0

    while counted_steps < rolled_number:
        cursor = (cursor - FIRST_CELL_INDEX + 1) % BOARD_SIZE + FIRST_CELL_INDEX
        guard += 1
        if guard > BOARD_SIZE * (rolled_number + 1):
            raise BoardNotReadyError

        if cursor == START_CELL_INDEX:
            passed_start = True

        if cursor in visited_cell_indexes:
            skipped_cells.append(cursor)
            continue

        counted_steps += 1

    return cursor, skipped_cells, passed_start


def roll_default_team_dice():
    team, state = get_or_create_default_team_state()
    if state is None:
        raise BoardNotReadyError

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        if state.is_quarantined:
            raise QuarantinedError
        if state.active_challenge_access_id is not None:
            raise TimerRunningError
        if state.dice_rolls_left <= 0:
            raise NoDiceRollsLeftError

        dice_a = random.randint(1, 6)
        dice_b = random.randint(1, 6)
        rolled_number = dice_a + dice_b
        previous_position = state.position_id
        current_position, skipped_cells, passed_start = calculate_position_with_skipped_cells(
            team,
            previous_position,
            rolled_number,
        )
        current_cell = Cell.objects.filter(cell_index=current_position).first()
        if current_cell is None:
            raise BoardNotReadyError

        start_reward = {
            "mileage_gained": START_PASS_MILEAGE_REWARD if passed_start else 0,
            "roll_gained": 0,
        }
        state.position = current_cell
        state.dice_rolls_left -= 1
        state.next_dice_reset_at = (
            timezone.now() + DICE_RECHARGE_INTERVAL if state.dice_rolls_left <= 0 else None
        )
        if passed_start:
            state.has_passed_start = True
        enter_quarantine_if_landed(state, current_cell)
        state_update_fields = [
            "position",
            "dice_rolls_left",
            "next_dice_reset_at",
            "is_quarantined",
            "quarantine_released_at",
            "updated_at",
        ]
        if passed_start:
            state_update_fields.append("has_passed_start")
        state.save(update_fields=state_update_fields)

        team_updates = {"board_position": current_position}
        if start_reward["mileage_gained"] > 0:
            team_updates["mileage_balance"] = F("mileage_balance") + start_reward["mileage_gained"]
        Team.objects.filter(pk=team.pk).update(**team_updates)

        roll = DiceRoll.objects.create(
            team=team,
            dice_a=dice_a,
            dice_b=dice_b,
            rolled_number=rolled_number,
            previous_position=previous_position,
            current_position=current_position,
        )

    state.position = current_cell
    team.board_position = current_position
    if start_reward["mileage_gained"] > 0:
        team.mileage_balance += start_reward["mileage_gained"]
    return (
        team,
        state,
        roll,
        get_event_for_cell(current_cell),
        skipped_cells,
        passed_start,
        start_reward,
    )


def reset_default_team_dice(rolls=1):
    team, state = get_or_create_default_team_state()
    if state is None:
        raise BoardNotReadyError

    state.dice_rolls_left = rolls
    state.next_dice_reset_at = None
    state.save(update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"])
    return team, state


def move_team_via_airport(destination_index):
    team, state = get_or_create_default_team_state()
    if state is None:
        raise BoardNotReadyError

    last_cell_index = FIRST_CELL_INDEX + BOARD_SIZE - 1
    if not isinstance(destination_index, int) or not (FIRST_CELL_INDEX <= destination_index <= last_cell_index):
        raise InvalidDestinationIndexError

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        if state.position.type != Cell.CellType.AIRPORT:
            raise NotAirportCellError
        if state.airport_move_used:
            raise AirportMoveAlreadyUsedError

        destination_cell = Cell.objects.filter(cell_index=destination_index).first()
        if destination_cell is None:
            raise BoardNotReadyError

        previous_position = state.position_id
        passed_start = destination_index == START_CELL_INDEX
        start_reward = {
            "mileage_gained": START_PASS_MILEAGE_REWARD if passed_start else 0,
            "roll_gained": 0,
        }
        state.position = destination_cell
        state.airport_move_used = True
        if passed_start:
            state.has_passed_start = True
        enter_quarantine_if_landed(state, destination_cell)
        update_fields = [
            "position",
            "airport_move_used",
            "is_quarantined",
            "quarantine_released_at",
            "updated_at",
        ]
        if passed_start:
            update_fields.append("has_passed_start")
        state.save(update_fields=update_fields)

        team_updates = {"board_position": destination_index}
        if start_reward["mileage_gained"] > 0:
            team_updates["mileage_balance"] = F("mileage_balance") + start_reward["mileage_gained"]
        Team.objects.filter(pk=team.pk).update(**team_updates)

    team.board_position = destination_index
    if start_reward["mileage_gained"] > 0:
        team.mileage_balance += start_reward["mileage_gained"]
    return (
        team,
        state,
        previous_position,
        destination_index,
        get_event_for_cell(destination_cell),
        passed_start,
        start_reward,
    )


def get_current_cell_candidates():
    team, state = get_or_create_default_team_state()
    if state is None:
        raise BoardNotReadyError

    cell = state.position
    if cell.type != Cell.CellType.CHALLENGE or not cell.difficulty:
        return team, state, cell, []
    if TeamChallengeAccess.objects.filter(team=team, source_cell=cell).exists():
        TeamCellCandidate.objects.filter(team=team, cell=cell).delete()
        return team, state, cell, []

    opened_challenge_ids = TeamChallengeAccess.objects.filter(team=team).values_list(
        "challenge_id",
        flat=True,
    )

    TeamCellCandidate.objects.filter(
        team=team,
        cell=cell,
        challenge_id__in=opened_challenge_ids,
    ).delete()

    existing_candidates = list(
        TeamCellCandidate.objects.filter(team=team, cell=cell)
        .select_related("challenge")
        .order_by("display_order")
    )

    missing_count = CHALLENGE_CANDIDATE_COUNT - len(existing_candidates)
    if missing_count > 0:
        existing_challenge_ids = [candidate.challenge_id for candidate in existing_candidates]
        available_challenges = list(
            Challenge.objects.filter(difficulty=cell.difficulty)
            .exclude(id__in=opened_challenge_ids)
            .exclude(id__in=existing_challenge_ids)
            .order_by("challenge_number")
        )
        selected_challenges = random.sample(
            available_challenges,
            min(missing_count, len(available_challenges)),
        )

        used_orders = {candidate.display_order for candidate in existing_candidates}
        free_orders = [
            order
            for order in range(1, CHALLENGE_CANDIDATE_COUNT + 1)
            if order not in used_orders
        ]

        for display_order, challenge in zip(free_orders, selected_challenges):
            existing_candidates.append(
                TeamCellCandidate.objects.create(
                    team=team,
                    cell=cell,
                    challenge=challenge,
                    display_order=display_order,
                )
            )

    existing_candidates.sort(key=lambda candidate: candidate.display_order)
    return team, state, cell, existing_candidates


def open_current_cell_challenge(challenge_id):
    team, state = get_or_create_default_team_state()
    if state is None:
        raise BoardNotReadyError

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        cell = state.position
        if cell.type != Cell.CellType.CHALLENGE:
            raise NotChallengeCellError
        if TeamChallengeAccess.objects.filter(team=team, source_cell=cell).exists():
            raise CellAlreadyOpenedError

        candidate = (
            TeamCellCandidate.objects.select_related("challenge")
            .filter(
                team=team,
                cell=cell,
                challenge_id=challenge_id,
            )
            .first()
        )
        if candidate is None:
            raise ChallengeNotCandidateError

        access, _ = TeamChallengeAccess.objects.get_or_create(
            team=team,
            challenge=candidate.challenge,
            defaults={"source_cell": cell},
        )

        now = timezone.now()
        candidate.status = TeamCellCandidate.Status.SELECTED
        candidate.selected_at = now
        candidate.save(update_fields=["status", "selected_at"])

        state.active_challenge_access = access
        state.save(update_fields=["active_challenge_access", "updated_at"])

    return team, state, access, now + timedelta(seconds=SOLVE_LIMIT_SECONDS)


def solve_default_team_active_challenge():
    team, state = get_or_create_default_team_state()
    if state is None:
        raise BoardNotReadyError
    if state.active_challenge_access_id is None:
        return team, state, None, False

    access = state.active_challenge_access
    is_extra_dice_granted = access.status != TeamChallengeAccess.Status.CLEARED
    if access.status != TeamChallengeAccess.Status.CLEARED:
        access.status = TeamChallengeAccess.Status.CLEARED
        access.cleared_at = timezone.now()
        access.save(update_fields=["status", "cleared_at"])
        state.dice_rolls_left += 1

    state.active_challenge_access = None
    update_fields = ["active_challenge_access", "updated_at"]
    if is_extra_dice_granted:
        update_fields.append("dice_rolls_left")
    state.save(update_fields=update_fields)
    return team, state, access, is_extra_dice_granted


def get_opened_challenges_summary():
    team, _ = get_or_create_default_team_state()
    accesses = list(
        TeamChallengeAccess.objects.filter(team=team)
        .select_related("challenge", "source_cell")
        .order_by("opened_at")
    )
    solved_accesses = [
        access
        for access in accesses
        if access.status == TeamChallengeAccess.Status.CLEARED
    ]

    return {
        "opened_challenges": [
            {
                "challenge_id": access.challenge_id,
                "cell_index": access.source_cell_id,
                "title": access.challenge.title,
                "category": access.challenge.category,
                "score": access.challenge.score,
                "is_solved": access.status == TeamChallengeAccess.Status.CLEARED,
                "solved_at": access.cleared_at,
                "opened_at": access.opened_at,
            }
            for access in accesses
        ],
        "total_count": len(accesses),
        "solved_count": len(solved_accesses),
        "total_score": sum(access.challenge.score for access in solved_accesses),
    }


def get_challenges_progress_summary():
    team, _ = get_or_create_default_team_state()
    accesses = list(
        TeamChallengeAccess.objects.filter(team=team)
        .select_related("challenge", "source_cell")
        .order_by("opened_at")
    )
    access_by_challenge_id = {access.challenge_id: access for access in accesses}
    challenges = list(Challenge.objects.order_by("challenge_number"))

    opened_count = 0
    solved_count = 0
    challenge_items = []
    for challenge in challenges:
        access = access_by_challenge_id.get(challenge.id)
        is_opened = access is not None
        is_solved = (
            access is not None
            and access.status == TeamChallengeAccess.Status.CLEARED
        )
        if is_opened:
            opened_count += 1
        if is_solved:
            solved_count += 1

        challenge_items.append({
            "challenge_id": challenge.id,
            "title": challenge.title,
            "category": challenge.category,
            "difficulty": challenge.difficulty,
            "score": challenge.score,
            "is_opened": is_opened,
            "is_solved": is_solved,
            "cell_index": access.source_cell_id if access is not None else None,
            "opened_at": access.opened_at if access is not None else None,
            "solved_at": access.cleared_at if access is not None else None,
        })

    return {
        "challenges": challenge_items,
        "total_count": len(challenge_items),
        "opened_count": opened_count,
        "solved_count": solved_count,
    }
