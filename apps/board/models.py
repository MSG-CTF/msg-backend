import uuid

from django.db import models


class Cell(models.Model):
    """ERD: cells — 고정 보드 배치, 팀별 진행 상태와 무관 (GET /api/v1/board)"""

    class CellType(models.TextChoices):
        START = "START"
        CHALLENGE = "CHALLENGE"
        CHANCE = "CHANCE"
        AIRPORT = "AIRPORT"
        QUARANTINE = "QUARANTINE"
        ROULETTE = "ROULETTE"

    class Difficulty(models.TextChoices):
        EASY = "EASY"
        MEDIUM = "MEDIUM"
        HARD = "HARD"

    cell_index = models.PositiveSmallIntegerField(primary_key=True)
    type = models.CharField(max_length=20, choices=CellType.choices)
    difficulty = models.CharField(max_length=10, choices=Difficulty.choices, null=True, blank=True)
    name = models.CharField(max_length=50)

    class Meta:
        db_table = "cells"
        ordering = ["cell_index"]

    def __str__(self):
        return f"{self.cell_index}: {self.name}"


class ChanceCard(models.Model):
    """ERD: chance_cards — 찬스칸 도착 시 뽑는 카드의 종류 정의 (GET /api/v1/board/chance/catalog)"""

    card_id = models.CharField(max_length=50, primary_key=True)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=500, blank=True)
    effect = models.CharField(max_length=50)
    is_pre_roll = models.BooleanField(default=True)
    weight = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "chance_cards"

    def __str__(self):
        return self.card_id


class Challenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge_number = models.PositiveSmallIntegerField(unique=True)
    title = models.CharField(max_length=100)
    category = models.CharField(max_length=20, default="MISC")
    difficulty = models.CharField(max_length=10, choices=Cell.Difficulty.choices)
    description = models.TextField(blank=True)
    flag = models.CharField(max_length=100, blank=True)
    score = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "challenges"
        ordering = ["challenge_number"]

    def __str__(self):
        return f"#{self.challenge_number} {self.title}"


class TeamChallengeAccess(models.Model):
    class Status(models.TextChoices):
        OPENED = "OPENED"
        CLEARED = "CLEARED"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.CASCADE,
        related_name="challenge_accesses",
    )
    challenge = models.ForeignKey(
        Challenge,
        on_delete=models.CASCADE,
        related_name="team_accesses",
    )
    source_cell = models.ForeignKey(
        Cell,
        to_field="cell_index",
        db_column="source_cell_index",
        on_delete=models.CASCADE,
        related_name="challenge_accesses",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPENED)
    opened_at = models.DateTimeField(auto_now_add=True)
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "team_challenge_accesses"
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "challenge"],
                name="unique_team_challenge_access",
            ),
            models.UniqueConstraint(
                fields=["team", "source_cell"],
                name="unique_team_cell_challenge_access",
            )
        ]
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "source_cell"]),
        ]

    def __str__(self):
        return f"{self.team_id}: {self.challenge_id} ({self.status})"


class TeamCellCandidate(models.Model):
    class Status(models.TextChoices):
        OFFERED = "OFFERED"
        SELECTED = "SELECTED"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.CASCADE,
        related_name="cell_candidates",
    )
    cell = models.ForeignKey(
        Cell,
        to_field="cell_index",
        db_column="cell_index",
        on_delete=models.CASCADE,
        related_name="team_candidates",
    )
    challenge = models.ForeignKey(
        Challenge,
        on_delete=models.CASCADE,
        related_name="cell_candidates",
    )
    display_order = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OFFERED)
    offered_at = models.DateTimeField(auto_now_add=True)
    selected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "team_cell_candidates"
        ordering = ["display_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "cell", "challenge"],
                name="unique_team_cell_candidate",
            ),
            models.UniqueConstraint(
                fields=["team", "cell", "display_order"],
                name="unique_team_cell_candidate_order",
            ),
        ]

    def __str__(self):
        return f"{self.team_id}: {self.cell_id} -> {self.challenge_id}"


class TeamBoardState(models.Model):
    """Single row per team for board progress."""

    team = models.OneToOneField(
        "teams.Team",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="board_state",
    )
    position = models.ForeignKey(
        Cell,
        to_field="cell_index",
        db_column="position",
        on_delete=models.PROTECT,
        related_name="team_states",
    )
    dice_rolls_left = models.PositiveSmallIntegerField(default=1)
    active_challenge_access = models.ForeignKey(
        TeamChallengeAccess,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_board_states",
    )
    is_quarantined = models.BooleanField(default=False)
    next_dice_reset_at = models.DateTimeField(null=True, blank=True)
    quarantine_released_at = models.DateTimeField(null=True, blank=True)
    quarantine_attempts_left = models.PositiveSmallIntegerField(default=0)
    airport_move_used = models.BooleanField(default=False)
    has_passed_start = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "team_board_states"

    def __str__(self):
        return f"{self.team_id}: {self.position_id}"


class DiceRoll(models.Model):
    """Audit log for dice rolls."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.CASCADE,
        related_name="dice_rolls",
    )
    dice_a = models.PositiveSmallIntegerField()
    dice_b = models.PositiveSmallIntegerField()
    rolled_number = models.PositiveSmallIntegerField()
    previous_position = models.PositiveSmallIntegerField()
    current_position = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "dice_rolls"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.team_id}: {self.rolled_number}"
