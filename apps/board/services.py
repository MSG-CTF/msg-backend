import random
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.models import Team
from apps.teams.models import MileageHistory, MileageType

from .exceptions import (
    AirportMoveAlreadyUsed,
    BoardCompleted,
    BoardNotReady,
    CardIdRequired,
    CellAlreadyOpened,
    ChallengeIdRequired,
    ChallengeNotCandidate,
    ChallengeNotSelected,
    ChanceCardAlreadyUsed,
    ChanceCardAwaitingDiscard,
    ChanceCardNotFound,
    ChanceCardWrongTiming,
    ChanceConfirmNotFound,
    InvalidDestinationIndex,
    NoCardToDiscard,
    NoPendingRoll,
    NoRollLeft,
    NotAirportCell,
    NotChallengeCell,
    NotChanceCell,
    NotQuarantined,
    NotRouletteCell,
    NotTeamLeader,
    PendingRollUnresolved,
    Quarantined,
    QuarantineCodeAlreadyUsed,
    QuarantineCodeInvalid,
    QuarantineCodeRequired,
    RouletteAlreadySpun,
    TimerRunning,
)
from .models import (
    Cell,
    Challenge,
    ChanceCard,
    DiceRoll,
    PendingDiceRoll,
    QuarantineEscapeCode,
    TeamBoardState,
    TeamCellCandidate,
    TeamCellConsumption,
    TeamChallengeAccess,
    TeamChanceCard,
)

BOARD_SIZE = 36
FIRST_CELL_INDEX = 1
LAST_CELL_INDEX = FIRST_CELL_INDEX + BOARD_SIZE - 1
START_CELL_INDEX = 1
CHALLENGE_CANDIDATE_COUNT = 3
SOLVE_LIMIT_SECONDS = 900
START_PASS_MILEAGE_REWARD = 100
START_ROLL_BONUS = 1
CHANCE_DRAW_ROLL_BONUS = 1
MOVE_OFFSET_MIN = -3
MOVE_OFFSET_MAX = 3
ROULETTE_REWARDS = (50, 100, 150, 200)
ROULETTE_REASON_PREFIX = "ROULETTE_CELL"
DICE_RECHARGE_INTERVAL = timedelta(minutes=15)
QUARANTINE_LOCK_INTERVAL = timedelta(minutes=15)


DEFAULT_TEAM_NAME = "test-team"


def get_default_team():
    """개발용 대시보드 프리뷰(DashboardView)에서만 쓰는 단일 팀. API 뷰는 request.user.team을 쓴다."""
    team, _ = Team.objects.get_or_create(team_name=DEFAULT_TEAM_NAME)
    return team


# ---------------------------------------------------------------------------
# 팀 상태 조회/초기화
# ---------------------------------------------------------------------------


def get_or_create_board_state(team):
    start_cell = Cell.objects.filter(cell_index=START_CELL_INDEX).first()
    if start_cell is None:
        raise BoardNotReady()

    state, _ = TeamBoardState.objects.get_or_create(
        team=team,
        defaults={"position": start_cell, "dice_rolls_left": 1},
    )
    state = apply_pending_dice_recharge(state)
    state = apply_pending_quarantine_release(state)
    return state


def apply_pending_dice_recharge(state):
    """15분 회복 타이머가 지났으면 주사위 1회만 채운다. 오래 기다려도 1회 이상 쌓이지 않는다."""
    if state.next_dice_reset_at is None or timezone.now() < state.next_dice_reset_at:
        return state

    state.dice_rolls_left += 1
    state.next_dice_reset_at = None
    state.save(update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"])
    return state


def grant_dice_roll(state, amount):
    """회복 타이머 밖에서 주사위를 지급한다. 대기 중인 회복 타이머가 있으면 같이 지운다.

    안 그러면 회복 타이머가 나중에 다시 발동해서(apply_pending_dice_recharge) 이미 채워진
    주사위에 1회를 더 얹어준다 — START 통과/찬스카드/문제 해결로 회복된 뒤에도 옛 타이머가
    남아 있던 사례에서 확인된 이중 지급 버그.
    """
    if amount <= 0:
        return
    state.dice_rolls_left += amount
    if state.dice_rolls_left > 0:
        state.next_dice_reset_at = None


def apply_pending_quarantine_release(state):
    if not state.is_quarantined or state.quarantine_released_at is None:
        return state
    if timezone.now() < state.quarantine_released_at:
        return state

    state.is_quarantined = False
    state.quarantine_released_at = None
    state.save(update_fields=["is_quarantined", "quarantine_released_at", "updated_at"])
    return state


def escape_quarantine_with_code(team, code):
    if not code:
        raise QuarantineCodeRequired()

    state = get_or_create_board_state(team)
    if not state.is_quarantined:
        raise NotQuarantined()

    with transaction.atomic():
        escape_code = QuarantineEscapeCode.objects.select_for_update().filter(code=code).first()
        if escape_code is None:
            raise QuarantineCodeInvalid()
        if escape_code.used_by_team_id is not None:
            raise QuarantineCodeAlreadyUsed()

        escape_code.used_by_team = team
        escape_code.used_at = timezone.now()
        escape_code.save(update_fields=["used_by_team", "used_at"])

        state.is_quarantined = False
        state.quarantine_released_at = None
        state.save(update_fields=["is_quarantined", "quarantine_released_at", "updated_at"])

    return {"is_quarantined": False}


def debug_force_release_quarantine(team):
    """로컬 프리뷰 전용. 15분 잠금을 기다리지 않고 즉시 무인도에서 풀어준다."""
    state = get_or_create_board_state(team)
    state.is_quarantined = False
    state.quarantine_released_at = None
    state.save(update_fields=["is_quarantined", "quarantine_released_at", "updated_at"])
    return state


def get_consumed_indexes(team):
    return set(TeamCellConsumption.objects.filter(team=team).values_list("cell_id", flat=True))


def is_board_completed(team):
    return TeamCellConsumption.objects.filter(team=team).count() >= BOARD_SIZE


def challenge_solve_deadline(access):
    return access.opened_at + timedelta(seconds=SOLVE_LIMIT_SECONDS)


def is_challenge_timer_running(access):
    return access.status == TeamChallengeAccess.Status.OPENED and timezone.now() < challenge_solve_deadline(
        access
    )


def compute_blocked_reason(team, state):
    if state.is_quarantined:
        return "QUARANTINED"
    if is_board_completed(team):
        return "BOARD_COMPLETED"

    cell = state.position
    if cell.type == Cell.CellType.CHALLENGE:
        access = TeamChallengeAccess.objects.filter(team=team, source_cell=cell).first()
        if access is None:
            return "CHALLENGE_NOT_SELECTED"
        if is_challenge_timer_running(access):
            return "TIMER_RUNNING"

    if PendingDiceRoll.objects.filter(team=team).exists():
        return "PENDING_CONFIRM"
    if state.dice_rolls_left <= 0:
        return "NO_ROLL_LEFT"
    return None


def get_event_for_cell(cell):
    if cell.type == Cell.CellType.START:
        return "NONE"
    return cell.type


def assert_team_leader(user):
    if user.team_id is None:
        raise NotTeamLeader()
    if not user.is_leader:
        raise NotTeamLeader()


def grant_mileage(team, amount, mileage_type, reason=None):
    if amount <= 0:
        return
    Team.objects.filter(pk=team.pk).update(mileage=F("mileage") + amount)
    team.mileage += amount
    MileageHistory.objects.create(team=team, type=mileage_type, amount=amount, reason=reason)


def roulette_reason(cell):
    return f"{ROULETTE_REASON_PREFIX}:{cell.cell_index}"


def consume_cell(team, cell):
    _, created = TeamCellConsumption.objects.get_or_create(team=team, cell=cell)
    return created


def enter_quarantine_if_landed(state, cell, is_first_visit):
    if cell.type != Cell.CellType.QUARANTINE or not is_first_visit:
        return
    state.is_quarantined = True
    state.quarantine_released_at = timezone.now() + QUARANTINE_LOCK_INTERVAL


def compute_start_reward(passed_start, landed_on_start):
    return {
        "mileage_gained": START_PASS_MILEAGE_REWARD if passed_start else 0,
        "roll_gained": START_ROLL_BONUS if landed_on_start else 0,
    }


def finalize_landing(team, state, cell, passed_start, landed_on_start):
    """칸 도착을 확정한다: 위치 갱신, 칸 소모, 무인도 진입, START 보상 지급."""
    state.position = cell
    if passed_start:
        state.has_passed_start = True
    is_first_visit = consume_cell(team, cell)
    enter_quarantine_if_landed(state, cell, is_first_visit)

    reward = compute_start_reward(passed_start, landed_on_start)
    update_fields = ["position", "is_quarantined", "quarantine_released_at", "updated_at"]
    if passed_start:
        update_fields.append("has_passed_start")
    if reward["roll_gained"]:
        grant_dice_roll(state, reward["roll_gained"])
        update_fields += ["dice_rolls_left", "next_dice_reset_at"]
    state.save(update_fields=update_fields)

    if reward["mileage_gained"]:
        grant_mileage(team, reward["mileage_gained"], MileageType.START_BONUS, reason="START 칸 통과")

    return reward


def _step(cursor, direction):
    return (cursor - FIRST_CELL_INDEX + direction) % BOARD_SIZE + FIRST_CELL_INDEX


def compute_movement(consumed_indexes, from_index, steps, direction=1):
    """물리 거리(steps)만큼 이동한 칸이 이미 소모됐으면, 소모되지 않은 칸이 나올 때까지 같은 방향으로 계속 진행한다."""
    movement_path = []
    cursor = from_index
    passed_start = False

    for _ in range(steps):
        cursor = _step(cursor, direction)
        if cursor == START_CELL_INDEX:
            passed_start = True
        movement_path.append(cursor)

    skipped_cells = []
    guard = 0
    while cursor in consumed_indexes:
        skipped_cells.append(cursor)
        cursor = _step(cursor, direction)
        if cursor == START_CELL_INDEX:
            passed_start = True
        movement_path.append(cursor)
        guard += 1
        if guard > BOARD_SIZE:
            raise BoardNotReady()

    return cursor, movement_path, skipped_cells, passed_start


def get_usable_post_roll_card(team):
    return (
        TeamChanceCard.objects.select_related("card")
        .filter(
            team=team,
            used_at__isnull=True,
            discarded_at__isnull=True,
            card__usage_timing=ChanceCard.UsageTiming.POST_ROLL,
        )
        .first()
    )


# ---------------------------------------------------------------------------
# 주사위
# ---------------------------------------------------------------------------


def _assert_can_roll(team, state):
    if state.is_quarantined:
        raise Quarantined()
    if is_board_completed(team):
        raise BoardCompleted()

    cell = state.position
    if cell.type == Cell.CellType.CHALLENGE:
        access = TeamChallengeAccess.objects.filter(team=team, source_cell=cell).first()
        if access is None:
            raise ChallengeNotSelected()
        if is_challenge_timer_running(access):
            raise TimerRunning()

    if PendingDiceRoll.objects.filter(team=team).exists():
        raise PendingRollUnresolved()
    if state.dice_rolls_left <= 0:
        raise NoRollLeft()


def roll_dice(team):
    state = get_or_create_board_state(team)

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        _assert_can_roll(team, state)

        dice_a = random.randint(1, 6)
        dice_b = random.randint(1, 6)
        rolled_number = dice_a + dice_b
        previous_position = state.position_id

        consumed = get_consumed_indexes(team)
        destination, movement_path, skipped_cells, passed_start = compute_movement(
            consumed, previous_position, rolled_number
        )
        landed_on_start = destination == START_CELL_INDEX
        destination_cell = Cell.objects.get(cell_index=destination)
        board_event_code = get_event_for_cell(destination_cell)

        state.dice_rolls_left -= 1
        state.next_dice_reset_at = (
            timezone.now() + DICE_RECHARGE_INTERVAL if state.dice_rolls_left <= 0 else None
        )
        state.save(update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"])

        DiceRoll.objects.create(
            team=team,
            dice_a=dice_a,
            dice_b=dice_b,
            rolled_number=rolled_number,
            previous_position=previous_position,
            current_position=destination,
        )

        held_card = get_usable_post_roll_card(team)
        if held_card is not None:
            PendingDiceRoll.objects.update_or_create(
                team=team,
                defaults={
                    "dice_a": dice_a,
                    "dice_b": dice_b,
                    "rolled_number": rolled_number,
                    "previous_position": previous_position,
                    "candidate_position": destination,
                    "movement_path": movement_path,
                    "skipped_cells": skipped_cells,
                    "passed_start": passed_start,
                    "board_event_code": board_event_code,
                },
            )
            start_reward = compute_start_reward(passed_start, landed_on_start)
            return {
                "dice_a": dice_a,
                "dice_b": dice_b,
                "rolled_number": rolled_number,
                "previous_position": previous_position,
                "current_position": destination,
                "movement_path": movement_path,
                "skipped_cells": skipped_cells,
                "passed_start": passed_start,
                "start_reward": start_reward,
                "board_event_code": board_event_code,
                "pending_confirm": True,
                "usable_chance_card": {"card_id": held_card.card_id, "effect": held_card.card.effect},
            }

        start_reward = finalize_landing(team, state, destination_cell, passed_start, landed_on_start)

    return {
        "dice_a": dice_a,
        "dice_b": dice_b,
        "rolled_number": rolled_number,
        "previous_position": previous_position,
        "current_position": destination,
        "movement_path": movement_path,
        "skipped_cells": skipped_cells,
        "passed_start": passed_start,
        "start_reward": start_reward,
        "board_event_code": board_event_code,
        "pending_confirm": False,
        "usable_chance_card": None,
    }


def confirm_dice_roll(team):
    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        pending = PendingDiceRoll.objects.filter(team=team).first()
        if pending is None:
            raise NoPendingRoll()
        if TeamChanceCard.objects.filter(
            team=team,
            used_at__isnull=True,
            discarded_at__isnull=True,
            pending_first_number__isnull=False,
        ).exists():
            raise ChanceConfirmNotFound()

        destination_cell = Cell.objects.get(cell_index=pending.candidate_position)
        landed_on_start = pending.candidate_position == START_CELL_INDEX
        start_reward = finalize_landing(team, state, destination_cell, pending.passed_start, landed_on_start)

        result = {
            "dice_a": pending.dice_a,
            "dice_b": pending.dice_b,
            "rolled_number": pending.rolled_number,
            "previous_position": pending.previous_position,
            "current_position": pending.candidate_position,
            "movement_path": pending.movement_path,
            "skipped_cells": pending.skipped_cells,
            "passed_start": pending.passed_start,
            "start_reward": start_reward,
            "board_event_code": pending.board_event_code,
            "pending_confirm": False,
            "usable_chance_card": None,
        }
        pending.delete()

    return result


def move_team_via_airport(team, destination_index):
    state = get_or_create_board_state(team)

    if isinstance(destination_index, bool) or not isinstance(destination_index, int):
        raise InvalidDestinationIndex()
    if not (FIRST_CELL_INDEX <= destination_index <= LAST_CELL_INDEX):
        raise InvalidDestinationIndex()

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        if state.position.type != Cell.CellType.AIRPORT:
            raise NotAirportCell()
        if state.airport_move_used:
            raise AirportMoveAlreadyUsed()

        consumed = get_consumed_indexes(team)
        if destination_index in consumed:
            raise InvalidDestinationIndex()

        destination_cell = Cell.objects.filter(cell_index=destination_index).first()
        if destination_cell is None:
            raise InvalidDestinationIndex()

        previous_position = state.position_id
        passed_start = destination_index == START_CELL_INDEX
        landed_on_start = passed_start

        state.airport_move_used = True
        state.save(update_fields=["airport_move_used", "updated_at"])

        start_reward = finalize_landing(team, state, destination_cell, passed_start, landed_on_start)

    return {
        "previous_position": previous_position,
        "current_position": destination_index,
        "movement_path": [destination_index],
        "board_event_code": get_event_for_cell(destination_cell),
        "passed_start": passed_start,
        "start_reward": start_reward,
    }


def spin_roulette(team):
    state = get_or_create_board_state(team)

    with transaction.atomic():
        locked_team = Team.objects.select_for_update().get(pk=team.pk)
        state = TeamBoardState.objects.select_for_update().select_related("position").get(team=team)
        cell = state.position
        if cell.type != Cell.CellType.ROULETTE:
            raise NotRouletteCell()

        reason = roulette_reason(cell)
        if MileageHistory.objects.filter(
            team=locked_team,
            type=MileageType.ROULETTE,
            reason=reason,
        ).exists():
            raise RouletteAlreadySpun()

        amount = random.choice(ROULETTE_REWARDS)
        consume_cell(locked_team, cell)
        grant_mileage(locked_team, amount, MileageType.ROULETTE, reason=reason)

    return {
        "roulette_result": {
            "label": f"마일리지 {amount}",
        },
        "mileage_gained": amount,
        "total_mileage": locked_team.mileage,
    }


# ---------------------------------------------------------------------------
# 문제 칸
# ---------------------------------------------------------------------------


def get_current_cell_candidates(team):
    state = get_or_create_board_state(team)
    if PendingDiceRoll.objects.filter(team=team).exists():
        raise PendingRollUnresolved()

    cell = state.position
    if cell.type != Cell.CellType.CHALLENGE or not cell.difficulty:
        return state, cell, []
    if TeamChallengeAccess.objects.filter(team=team, source_cell=cell).exists():
        TeamCellCandidate.objects.filter(team=team, cell=cell).delete()
        return state, cell, []

    opened_challenge_ids = TeamChallengeAccess.objects.filter(team=team).values_list(
        "challenge_id", flat=True
    )

    TeamCellCandidate.objects.filter(
        team=team, cell=cell, challenge_id__in=opened_challenge_ids
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
            available_challenges, min(missing_count, len(available_challenges))
        )

        used_orders = {candidate.display_order for candidate in existing_candidates}
        free_orders = [
            order for order in range(1, CHALLENGE_CANDIDATE_COUNT + 1) if order not in used_orders
        ]

        for display_order, challenge in zip(free_orders, selected_challenges):
            existing_candidates.append(
                TeamCellCandidate.objects.create(
                    team=team, cell=cell, challenge=challenge, display_order=display_order
                )
            )

    existing_candidates.sort(key=lambda candidate: candidate.display_order)
    return state, cell, existing_candidates


def open_current_cell_challenge(team, challenge_id):
    if challenge_id is None:
        raise ChallengeIdRequired()

    state = get_or_create_board_state(team)

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        if PendingDiceRoll.objects.filter(team=team).exists():
            raise PendingRollUnresolved()
        cell = state.position
        if cell.type != Cell.CellType.CHALLENGE:
            raise NotChallengeCell()
        if TeamChallengeAccess.objects.filter(team=team, source_cell=cell).exists():
            raise CellAlreadyOpened()

        candidate = (
            TeamCellCandidate.objects.select_related("challenge")
            .filter(team=team, cell=cell, challenge_id=challenge_id)
            .first()
        )
        if candidate is None:
            raise ChallengeNotCandidate()

        access, _ = TeamChallengeAccess.objects.get_or_create(
            team=team, challenge=candidate.challenge, defaults={"source_cell": cell}
        )

        now = timezone.now()
        candidate.status = TeamCellCandidate.Status.SELECTED
        candidate.selected_at = now
        candidate.save(update_fields=["status", "selected_at"])

        state.active_challenge_access = access
        state.save(update_fields=["active_challenge_access", "updated_at"])

    return access, now + timedelta(seconds=SOLVE_LIMIT_SECONDS)


def solve_active_challenge(team):
    state = get_or_create_board_state(team)
    if state.active_challenge_access_id is None:
        return state, None, False

    access = state.active_challenge_access
    is_extra_dice_granted = access.status != TeamChallengeAccess.Status.CLEARED
    if is_extra_dice_granted:
        access.status = TeamChallengeAccess.Status.CLEARED
        access.cleared_at = timezone.now()
        access.save(update_fields=["status", "cleared_at"])
        grant_dice_roll(state, 1)

    state.active_challenge_access = None
    update_fields = ["active_challenge_access", "updated_at"]
    if is_extra_dice_granted:
        update_fields += ["dice_rolls_left", "next_dice_reset_at"]
    state.save(update_fields=update_fields)
    return state, access, is_extra_dice_granted


def get_opened_challenges_summary(team):
    accesses = list(
        TeamChallengeAccess.objects.filter(team=team)
        .select_related("challenge", "source_cell")
        .order_by("opened_at")
    )
    solved_accesses = [
        access for access in accesses if access.status == TeamChallengeAccess.Status.CLEARED
    ]

    return {
        "opened_challenges": [
            {
                "challenge_id": access.challenge_id,
                "cell_index": access.source_cell_id,
                "title": access.challenge.title,
                "category": access.challenge.category,
                "club_name": access.challenge.club_name,
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


def get_challenges_progress_summary(team):
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
        is_solved = access is not None and access.status == TeamChallengeAccess.Status.CLEARED
        if is_opened:
            opened_count += 1
        if is_solved:
            solved_count += 1

        challenge_items.append(
            {
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
            }
        )

    return {
        "challenges": challenge_items,
        "total_count": len(challenge_items),
        "opened_count": opened_count,
        "solved_count": solved_count,
    }


def build_cell_states(team):
    consumed_indexes = sorted(get_consumed_indexes(team))
    accesses = {
        access.source_cell_id: access
        for access in TeamChallengeAccess.objects.filter(team=team).select_related("challenge")
    }

    states = []
    for cell_index in consumed_indexes:
        access = accesses.get(cell_index)
        if access is not None and access.status == TeamChallengeAccess.Status.CLEARED:
            status = "CLEARED"
        elif access is not None and access.status == TeamChallengeAccess.Status.OPENED:
            status = "OPENED"
        else:
            status = "CONSUMED"
        states.append(
            {
                "cell_index": cell_index,
                "status": status,
                "category": access.challenge.category if access is not None else None,
            }
        )
    return states, consumed_indexes


# ---------------------------------------------------------------------------
# 찬스카드
# ---------------------------------------------------------------------------


def _card_usable_now(card, state, blocked_reason, has_pending):
    if card.usage_timing == ChanceCard.UsageTiming.QUARANTINE_STATE:
        return state.is_quarantined
    if state.is_quarantined:
        return False
    if card.usage_timing == ChanceCard.UsageTiming.POST_ROLL:
        return has_pending
    # PRE_ROLL
    if has_pending:
        return False
    if blocked_reason is None:
        return True
    return card.effect == "GRANT_EXTRA_ROLL" and blocked_reason == "NO_ROLL_LEFT"


def _held_cards_queryset(team):
    """아직 쓰지도 버리지도 않은, 현재 보유 중인 찬스카드."""
    return TeamChanceCard.objects.filter(team=team, used_at__isnull=True, discarded_at__isnull=True)


def build_chance_cards_view(team, state):
    draws = TeamChanceCard.objects.select_related("card").filter(team=team).order_by("drawn_at")
    blocked_reason = compute_blocked_reason(team, state)
    has_pending = PendingDiceRoll.objects.filter(team=team).exists()
    awaiting_discard = _held_cards_queryset(team).count() >= 2

    result = []
    for draw in draws:
        used = draw.used_at is not None
        discarded = draw.discarded_at is not None
        usable_now = (
            _card_usable_now(draw.card, state, blocked_reason, has_pending)
            if not used and not discarded and not awaiting_discard
            else False
        )
        result.append(
            {
                "card_id": draw.card_id,
                "used": used,
                "discarded": discarded,
                "usable_now": usable_now,
            }
        )
    return result


def draw_chance_card(team):
    get_or_create_board_state(team)
    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        if PendingDiceRoll.objects.filter(team=team).exists():
            raise PendingRollUnresolved()
        cell = state.position
        if cell.type != Cell.CellType.CHANCE:
            raise NotChanceCell()
        if TeamChanceCard.objects.filter(team=team, source_cell=cell).exists():
            raise NotChanceCell()

        card = random.choice(list(ChanceCard.objects.all()))
        draw = TeamChanceCard.objects.create(team=team, source_cell=cell, card=card)

        consume_cell(team, cell)
        grant_dice_roll(state, CHANCE_DRAW_ROLL_BONUS)
        state.save(update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"])

    awaiting_discard = _held_cards_queryset(team).count() >= 2
    return draw, state.dice_rolls_left, awaiting_discard


def discard_chance_card(team, card_id):
    """찬스카드를 2장 들고 있을 때(재드로우), 팀장이 직접 하나를 골라 버린다."""
    if not card_id:
        raise CardIdRequired()

    with transaction.atomic():
        held = list(_held_cards_queryset(team).select_for_update().select_related("card"))
        if len(held) < 2:
            raise NoCardToDiscard()

        target = next((draw for draw in held if draw.card_id == card_id), None)
        if target is None:
            raise ChanceCardNotFound()

        target.discarded_at = timezone.now()
        target.save(update_fields=["discarded_at"])

        remaining = next(draw for draw in held if draw.pk != target.pk)

    return {"discarded_card_id": target.card_id, "kept_card_id": remaining.card_id}


def _assert_timing_ok(team, state, card):
    if _held_cards_queryset(team).count() >= 2:
        raise ChanceCardAwaitingDiscard()
    blocked_reason = compute_blocked_reason(team, state)
    has_pending = PendingDiceRoll.objects.filter(team=team).exists()
    if not _card_usable_now(card, state, blocked_reason, has_pending):
        raise ChanceCardWrongTiming()


def _effect_reroll(team, state, draw, payload):
    pending = PendingDiceRoll.objects.select_for_update().filter(team=team).first()
    if pending is None:
        raise ChanceCardWrongTiming()

    previous_position = pending.previous_position
    dice_a = random.randint(1, 6)
    dice_b = random.randint(1, 6)
    rolled_number = dice_a + dice_b
    consumed = get_consumed_indexes(team)
    destination, movement_path, skipped_cells, passed_start = compute_movement(
        consumed, previous_position, rolled_number
    )
    landed_on_start = destination == START_CELL_INDEX
    destination_cell = Cell.objects.get(cell_index=destination)

    DiceRoll.objects.create(
        team=team, dice_a=dice_a, dice_b=dice_b, rolled_number=rolled_number,
        previous_position=previous_position, current_position=destination,
    )

    draw.used_at = timezone.now()
    draw.save(update_fields=["used_at"])
    pending.delete()

    finalize_landing(team, state, destination_cell, passed_start, landed_on_start)

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "dice_a": dice_a,
        "dice_b": dice_b,
        "rolled_number": rolled_number,
        "from_index": previous_position,
        "to_index": destination,
        "movement_path": movement_path,
        "skipped_cells": skipped_cells,
        "used": True,
    }


def _effect_roll_twice_choose(team, state, draw, payload):
    _assert_can_roll(team, state)

    first_a = random.randint(1, 6)
    first_b = random.randint(1, 6)
    first_number = first_a + first_b
    second_a = random.randint(1, 6)
    second_b = random.randint(1, 6)
    second_number = second_a + second_b
    previous_position = state.position_id
    consumed = get_consumed_indexes(team)
    destination, movement_path, skipped_cells, passed_start = compute_movement(
        consumed, previous_position, first_number
    )
    second_destination, _, _, _ = compute_movement(consumed, previous_position, second_number)
    destination_cell = Cell.objects.get(cell_index=destination)

    state.dice_rolls_left -= 1
    state.next_dice_reset_at = (
        timezone.now() + DICE_RECHARGE_INTERVAL if state.dice_rolls_left <= 0 else None
    )
    state.save(update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"])

    DiceRoll.objects.create(
        team=team,
        dice_a=first_a,
        dice_b=first_b,
        rolled_number=first_number,
        previous_position=previous_position,
        current_position=destination,
    )
    DiceRoll.objects.create(
        team=team,
        dice_a=second_a,
        dice_b=second_b,
        rolled_number=second_number,
        previous_position=previous_position,
        current_position=second_destination,
    )
    PendingDiceRoll.objects.create(
        team=team,
        dice_a=first_a,
        dice_b=first_b,
        rolled_number=first_number,
        previous_position=previous_position,
        candidate_position=destination,
        movement_path=movement_path,
        skipped_cells=skipped_cells,
        passed_start=passed_start,
        board_event_code=get_event_for_cell(destination_cell),
    )

    draw.pending_first_number = first_number
    draw.pending_second_number = second_number
    draw.save(update_fields=["pending_first_number", "pending_second_number"])

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "first_number": first_number,
        "second_number": second_number,
        "awaiting_confirm": True,
        "used": False,
    }


def _effect_move_offset(team, state, draw, payload):
    """POST_ROLL 전용 카드. 방금 굴린(아직 미확정) 도착 후보 위치를 기준으로 추가 이동한다."""
    pending = PendingDiceRoll.objects.filter(team=team).first()
    if pending is None:
        raise ChanceCardWrongTiming()

    offset = payload.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise InvalidDestinationIndex()
    if offset == 0 or not (MOVE_OFFSET_MIN <= offset <= MOVE_OFFSET_MAX):
        raise InvalidDestinationIndex()

    base_position = pending.candidate_position
    consumed = get_consumed_indexes(team)
    direction = 1 if offset > 0 else -1
    destination, movement_path, skipped_cells, extra_passed_start = compute_movement(
        consumed, base_position, abs(offset), direction=direction
    )
    passed_start = pending.passed_start or extra_passed_start
    landed_on_start = destination == START_CELL_INDEX
    destination_cell = Cell.objects.get(cell_index=destination)

    draw.used_at = timezone.now()
    draw.save(update_fields=["used_at"])
    pending.delete()

    finalize_landing(team, state, destination_cell, passed_start, landed_on_start)

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "from_index": base_position,
        "to_index": destination,
        "movement_path": movement_path,
        "skipped_cells": skipped_cells,
        "used": True,
    }


def _effect_free_move(team, state, draw, payload):
    destination = payload.get("destination_index")
    if isinstance(destination, bool) or not isinstance(destination, int):
        raise InvalidDestinationIndex()
    if not (FIRST_CELL_INDEX <= destination <= LAST_CELL_INDEX):
        raise InvalidDestinationIndex()

    consumed = get_consumed_indexes(team)
    if destination in consumed:
        raise InvalidDestinationIndex()

    destination_cell = Cell.objects.filter(cell_index=destination).first()
    if destination_cell is None:
        raise InvalidDestinationIndex()

    current_position = state.position_id
    passed_start = destination == START_CELL_INDEX

    draw.used_at = timezone.now()
    draw.save(update_fields=["used_at"])

    finalize_landing(team, state, destination_cell, passed_start, passed_start)

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "from_index": current_position,
        "to_index": destination,
        "movement_path": [destination],
        "skipped_cells": [],
        "used": True,
    }


def _effect_grant_extra_roll(team, state, draw, payload):
    grant_dice_roll(state, 1)
    state.save(update_fields=["dice_rolls_left", "next_dice_reset_at", "updated_at"])

    draw.used_at = timezone.now()
    draw.save(update_fields=["used_at"])

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "dice_rolls_left": state.dice_rolls_left,
        "used": True,
    }


def _effect_quarantine_escape_free(team, state, draw, payload):
    state.is_quarantined = False
    state.quarantine_released_at = None
    grant_dice_roll(state, 1)
    state.save(update_fields=[
        "is_quarantined",
        "quarantine_released_at",
        "dice_rolls_left",
        "next_dice_reset_at",
        "updated_at",
    ])

    draw.used_at = timezone.now()
    draw.save(update_fields=["used_at"])

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "is_quarantined": False,
        "dice_rolls_left": state.dice_rolls_left,
        "used": True,
    }


def _effect_force_move_to_quarantine(team, state, draw, payload):
    quarantine_cell = Cell.objects.get(type=Cell.CellType.QUARANTINE)
    current_position = state.position_id

    draw.used_at = timezone.now()
    draw.save(update_fields=["used_at"])

    finalize_landing(team, state, quarantine_cell, passed_start=False, landed_on_start=False)

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "from_index": current_position,
        "to_index": quarantine_cell.cell_index,
        "movement_path": [quarantine_cell.cell_index],
        "skipped_cells": [],
        "used": True,
    }


_EFFECT_HANDLERS = {
    "RE_ROLL": _effect_reroll,
    "ROLL_TWICE_CHOOSE": _effect_roll_twice_choose,
    "MOVE_OFFSET": _effect_move_offset,
    "FREE_MOVE": _effect_free_move,
    "GRANT_EXTRA_ROLL": _effect_grant_extra_roll,
    "QUARANTINE_ESCAPE_FREE": _effect_quarantine_escape_free,
    "FORCE_MOVE_TO_QUARANTINE": _effect_force_move_to_quarantine,
}


def use_chance_card(team, card_id, payload):
    if not card_id:
        raise CardIdRequired()

    existing_draw = TeamChanceCard.objects.filter(team=team, card_id=card_id).first()
    if existing_draw is None:
        raise ChanceCardNotFound()
    if existing_draw.used_at is not None:
        raise ChanceCardAlreadyUsed()
    if existing_draw.discarded_at is not None:
        raise ChanceCardNotFound()
    get_or_create_board_state(team)

    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        draw = (
            TeamChanceCard.objects.select_related("card")
            .filter(team=team, card_id=card_id)
            .first()
        )
        if draw is None:
            raise ChanceCardNotFound()
        if draw.used_at is not None:
            raise ChanceCardAlreadyUsed()
        if draw.discarded_at is not None:
            raise ChanceCardNotFound()

        card = draw.card
        _assert_timing_ok(team, state, card)

        handler = _EFFECT_HANDLERS.get(card.effect)
        if handler is None:
            raise ChanceCardNotFound()

        return handler(team, state, draw, payload or {})


def confirm_chance_choice(team, choice):
    if choice not in ("FIRST", "SECOND"):
        raise ChanceConfirmNotFound()

    get_or_create_board_state(team)
    with transaction.atomic():
        state = TeamBoardState.objects.select_for_update().get(team=team)
        draw = (
            TeamChanceCard.objects.select_related("card")
            .filter(
                team=team,
                used_at__isnull=True,
                discarded_at__isnull=True,
                pending_first_number__isnull=False,
            )
            .first()
        )
        pending = PendingDiceRoll.objects.filter(team=team).first()
        if draw is None or pending is None:
            raise ChanceConfirmNotFound()

        chosen_number = draw.pending_first_number if choice == "FIRST" else draw.pending_second_number
        previous_position = pending.previous_position
        consumed = get_consumed_indexes(team)
        destination, movement_path, skipped_cells, passed_start = compute_movement(
            consumed, previous_position, chosen_number
        )
        landed_on_start = destination == START_CELL_INDEX
        destination_cell = Cell.objects.get(cell_index=destination)

        draw.used_at = timezone.now()
        draw.pending_first_number = None
        draw.pending_second_number = None
        draw.save(update_fields=["used_at", "pending_first_number", "pending_second_number"])
        pending.delete()

        finalize_landing(team, state, destination_cell, passed_start, landed_on_start)

    return {
        "card_id": draw.card_id,
        "effect": draw.card.effect,
        "choice": choice,
        "chosen_number": chosen_number,
        "from_index": previous_position,
        "to_index": destination,
        "used": True,
    }
