import uuid

from django.db import DatabaseError
from django.utils import timezone
from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.board.models import Cell, ChanceCard
from apps.board.serializers import (
    CellSerializer,
    ChallengeCandidateSerializer,
    ChanceCardSerializer,
    TeamChallengeAccessSerializer,
    TeamBoardStateSerializer,
)
from apps.board.services import (
    AirportMoveAlreadyUsedError,
    BoardNotReadyError,
    CellAlreadyOpenedError,
    ChallengeNotCandidateError,
    InvalidDestinationIndexError,
    NoDiceRollsLeftError,
    NotAirportCellError,
    NotChallengeCellError,
    QuarantinedError,
    TimerRunningError,
    get_challenges_progress_summary,
    get_current_cell_candidates,
    get_opened_challenges_summary,
    get_or_create_default_team_state,
    move_team_via_airport,
    open_current_cell_challenge,
    reset_default_team_dice,
    roll_default_team_dice,
    solve_default_team_active_challenge,
)


class DashboardView(TemplateView):
    template_name = "board/dashboard.html"


class BoardView(ListAPIView):
    """GET /api/v1/board returns the fixed 36-cell board."""

    queryset = Cell.objects.all()
    serializer_class = CellSerializer

    def list(self, request, *args, **kwargs):
        try:
            cells = self.get_serializer(self.get_queryset(), many=True).data
        except DatabaseError:
            cells = []

        if not cells:
            return Response(
                {
                    "code": "BOARD_LOAD_FAILED",
                    "message": "보드 정보를 불러오지 못했습니다.",
                    "data": None,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({
            "code": "SUCCESS",
            "message": "성공",
            "data": {
                "total_cell_count": len(cells),
                "cells": cells,
            },
        })


class BoardMeView(APIView):
    """GET /api/v1/board/me returns the current team's board state."""

    def get(self, request, *args, **kwargs):
        _, board_state = get_or_create_default_team_state()
        if board_state is None:
            return Response(
                {
                    "code": "BOARD_NOT_READY",
                    "message": "보드를 먼저 시드해야 합니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        active_access = board_state.active_challenge_access
        return Response({
            "code": "SUCCESS",
            "message": "성공",
            "data": {
                "position": board_state.position_id,
                "type": board_state.position.type,
                "is_quarantined": board_state.is_quarantined,
                "dice_rolls_left": board_state.dice_rolls_left,
                "next_dice_reset_at": board_state.next_dice_reset_at,
                "quarantine_attempts_left": board_state.quarantine_attempts_left,
                "airport_move_used": board_state.airport_move_used,
                "has_passed_start": board_state.has_passed_start,
                "chance_cards": [],
                "active_challenge": (
                    {
                        "challenge_id": active_access.challenge_id,
                        "opened_at": active_access.opened_at,
                    }
                    if active_access is not None
                    else None
                ),
            },
        })


class CellCurrentView(APIView):
    """GET /api/v1/board/cell/current returns current cell and challenge candidates."""

    def get(self, request, *args, **kwargs):
        try:
            _, _, cell, candidates = get_current_cell_candidates()
        except BoardNotReadyError:
            return Response(
                {
                    "code": "BOARD_NOT_READY",
                    "message": "보드를 먼저 시드해야 합니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            "code": "SUCCESS",
            "message": "성공",
            "data": {
                "cell_index": cell.cell_index,
                "type": cell.type,
                "challenge_candidates": [
                    ChallengeCandidateSerializer(candidate.challenge).data
                    for candidate in candidates
                ],
            },
        })


class CellOpenView(APIView):
    """POST /api/v1/board/cell/open opens one candidate from the current cell."""

    def post(self, request, *args, **kwargs):
        challenge_id = request.data.get("challenge_id")
        if challenge_id is None:
            return Response(
                {
                    "code": "CHALLENGE_ID_REQUIRED",
                    "message": "challenge_id가 필요합니다.",
                    "data": None,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            challenge_id = uuid.UUID(str(challenge_id))
            _, _, access, solve_deadline_at = open_current_cell_challenge(challenge_id)
        except ValueError:
            return Response(
                {
                    "code": "CHALLENGE_ID_REQUIRED",
                    "message": "challenge_id가 필요합니다.",
                    "data": None,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except BoardNotReadyError:
            return Response(
                {
                    "code": "BOARD_NOT_READY",
                    "message": "보드를 먼저 시드해야 합니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except NotChallengeCellError:
            return Response(
                {
                    "code": "NOT_CHALLENGE_CELL",
                    "message": "현재 위치가 문제 칸이 아닙니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except CellAlreadyOpenedError:
            return Response(
                {
                    "code": "CELL_ALREADY_OPENED",
                    "message": "현재 칸에서는 이미 문제를 열었습니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except ChallengeNotCandidateError:
            return Response(
                {
                    "code": "CHALLENGE_NOT_CANDIDATE",
                    "message": "현재 칸에서 선택할 수 없는 문제입니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            "code": "SUCCESS",
            "message": "성공",
            "data": {
                "cell_index": access.source_cell_id,
                "challenge_id": access.challenge_id,
                "opened_at": access.opened_at,
                "solve_deadline_at": solve_deadline_at,
                "remaining_seconds": 900,
            },
        })


class ChanceCardCatalogView(ListAPIView):
    """GET /api/v1/board/chance/catalog returns chance card definitions."""

    queryset = ChanceCard.objects.all()
    serializer_class = ChanceCardSerializer

    def list(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_queryset(), many=True)
        return Response({
            "code": "SUCCESS",
            "message": "성공",
            "data": {
                "cards": serializer.data,
                "total_count": len(serializer.data),
            },
        })


class DiceStatusView(APIView):
    """GET /api/v1/board/dice/status for the single-team development mode."""

    def get(self, request, *args, **kwargs):
        _, board_state = get_or_create_default_team_state()
        if board_state is None:
            return Response(
                {
                    "code": "BOARD_NOT_READY",
                    "message": "보드를 먼저 시드해야 합니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        timer_running = board_state.active_challenge_access_id is not None
        blocked_reason = None
        if board_state.is_quarantined:
            blocked_reason = "QUARANTINED"
        elif timer_running:
            blocked_reason = "TIMER_RUNNING"
        elif board_state.dice_rolls_left <= 0:
            blocked_reason = "NO_ROLL_LEFT"

        return Response({
            "code": "SUCCESS",
            "message": "성공",
            "data": {
                "can_roll": blocked_reason is None,
                "dice_rolls_left": board_state.dice_rolls_left,
                "is_quarantined": board_state.is_quarantined,
                "timer_running": timer_running,
                "blocked_reason": blocked_reason,
                "server_time": timezone.now(),
                "next_dice_reset_at": board_state.next_dice_reset_at,
                "quarantine_released_at": board_state.quarantine_released_at,
            },
        })


class DiceRollView(APIView):
    """POST /api/v1/board/dice/roll for the single-team development mode."""

    def post(self, request, *args, **kwargs):
        try:
            (
                _,
                _,
                roll,
                event_triggered,
                skipped_cells,
                passed_start,
                start_reward,
            ) = roll_default_team_dice()
        except BoardNotReadyError:
            return Response(
                {
                    "code": "BOARD_NOT_READY",
                    "message": "보드를 먼저 시드해야 합니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except QuarantinedError:
            return Response(
                {
                    "code": "QUARANTINED",
                    "message": "무인도 상태에서는 주사위를 굴릴 수 없습니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except TimerRunningError:
            return Response(
                {
                    "code": "TIMER_RUNNING",
                    "message": "진행 중인 문제가 있어 주사위를 굴릴 수 없습니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except NoDiceRollsLeftError:
            return Response(
                {
                    "code": "NO_ROLL_LEFT",
                    "message": "남은 주사위 횟수가 없습니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            "code": "SUCCESS",
            "message": "성공",
            "data": {
                "dice_a": roll.dice_a,
                "dice_b": roll.dice_b,
                "rolled_number": roll.rolled_number,
                "previous_position": roll.previous_position,
                "current_position": roll.current_position,
                "skipped_cells": skipped_cells,
                "passed_start": passed_start,
                "start_reward": start_reward,
                "board_event_code": event_triggered,
            },
        })


class AirportMoveView(APIView):
    """POST /api/v1/board/airport/move moves the team from the airport cell to a chosen destination."""

    def post(self, request, *args, **kwargs):
        destination_index = request.data.get("destination_index")
        if isinstance(destination_index, bool) or not isinstance(destination_index, int):
            return Response(
                {
                    "code": "INVALID_DESTINATION_INDEX",
                    "message": "이동할 수 없는 칸입니다.",
                    "data": None,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            (
                _,
                _,
                previous_position,
                current_position,
                event_triggered,
                passed_start,
                start_reward,
            ) = move_team_via_airport(destination_index)
        except InvalidDestinationIndexError:
            return Response(
                {
                    "code": "INVALID_DESTINATION_INDEX",
                    "message": "이동할 수 없는 칸입니다.",
                    "data": None,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except BoardNotReadyError:
            return Response(
                {
                    "code": "BOARD_NOT_READY",
                    "message": "보드를 먼저 시드해야 합니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except NotAirportCellError:
            return Response(
                {
                    "code": "NOT_AIRPORT_CELL",
                    "message": "현재 위치가 공항 칸이 아닙니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except AirportMoveAlreadyUsedError:
            return Response(
                {
                    "code": "AIRPORT_MOVE_ALREADY_USED",
                    "message": "이미 공항 이동을 사용했습니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            "code": "SUCCESS",
            "message": "성공",
            "data": {
                "previous_position": previous_position,
                "current_position": current_position,
                "board_event_code": event_triggered,
                "passed_start": passed_start,
                "start_reward": start_reward,
            },
        })


class DiceResetView(APIView):
    """POST /api/v1/board/dice/reset for local preview convenience."""

    def post(self, request, *args, **kwargs):
        try:
            _, board_state = reset_default_team_dice()
        except BoardNotReadyError:
            return Response(
                {
                    "code": "BOARD_NOT_READY",
                    "message": "보드를 먼저 시드해야 합니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            "code": "SUCCESS",
            "message": "주사위 리셋 성공",
            "data": {
                "can_roll": board_state.dice_rolls_left > 0,
                "dice_rolls_left": board_state.dice_rolls_left,
            },
        })


class OpenedChallengesView(APIView):
    """GET /api/v1/board/opened_challenges returns opened challenge list."""

    def get(self, request, *args, **kwargs):
        return Response({
            "code": "SUCCESS",
            "message": "열린 문제 목록 조회 성공",
            "data": get_opened_challenges_summary(),
        })


class ChallengeListView(APIView):
    """GET /api/v1/board/challenges returns all challenges with team progress."""

    def get(self, request, *args, **kwargs):
        return Response({
            "code": "SUCCESS",
            "message": "문제 목록 조회 성공",
            "data": get_challenges_progress_summary(),
        })


class ChallengeSolveView(APIView):
    """POST /api/v1/board/challenge/solve marks the active challenge as cleared."""

    def post(self, request, *args, **kwargs):
        try:
            (
                _,
                board_state,
                challenge_access,
                is_extra_dice_granted,
            ) = solve_default_team_active_challenge()
        except BoardNotReadyError:
            return Response(
                {
                    "code": "BOARD_NOT_READY",
                    "message": "보드를 먼저 시드해야 합니다.",
                    "data": None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            "code": "SUCCESS",
            "message": "문제 풀이 처리 성공",
            "data": {
                "solved_challenge": (
                    TeamChallengeAccessSerializer(challenge_access).data
                    if challenge_access is not None
                    else None
                ),
                "is_extra_dice_granted": is_extra_dice_granted,
                "dice_rolls_left": board_state.dice_rolls_left,
                "board_state": TeamBoardStateSerializer(board_state).data,
            },
        })
