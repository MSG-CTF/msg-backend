from django.contrib import admin

from apps.board.models import (
    Cell,
    Challenge,
    ChanceCard,
    DiceRoll,
    PendingDiceRoll,
    TeamBoardState,
    TeamCellCandidate,
    TeamCellConsumption,
    TeamChallengeAccess,
    TeamChanceCard,
)


@admin.register(Cell)
class CellAdmin(admin.ModelAdmin):
    list_display = ["cell_index", "type", "difficulty", "name"]


@admin.register(ChanceCard)
class ChanceCardAdmin(admin.ModelAdmin):
    list_display = ["card_id", "name", "effect", "usage_timing", "weight"]


@admin.register(TeamCellConsumption)
class TeamCellConsumptionAdmin(admin.ModelAdmin):
    list_display = ["id", "team", "cell", "consumed_at"]


@admin.register(TeamChanceCard)
class TeamChanceCardAdmin(admin.ModelAdmin):
    list_display = ["id", "team", "card", "source_cell", "drawn_at", "used_at"]


@admin.register(PendingDiceRoll)
class PendingDiceRollAdmin(admin.ModelAdmin):
    list_display = ["team", "previous_position", "candidate_position", "created_at"]


@admin.register(Challenge)
class ChallengeAdmin(admin.ModelAdmin):
    list_display = ["challenge_number", "title", "category", "difficulty", "score"]
    list_filter = ["category", "difficulty"]


@admin.register(TeamChallengeAccess)
class TeamChallengeAccessAdmin(admin.ModelAdmin):
    list_display = ["id", "team", "challenge", "source_cell", "status", "opened_at", "cleared_at"]
    list_filter = ["status"]


@admin.register(TeamCellCandidate)
class TeamCellCandidateAdmin(admin.ModelAdmin):
    list_display = ["id", "team", "cell", "challenge", "display_order", "status", "offered_at", "selected_at"]
    list_filter = ["status"]


@admin.register(TeamBoardState)
class TeamBoardStateAdmin(admin.ModelAdmin):
    list_display = [
        "team",
        "position",
        "dice_rolls_left",
        "active_challenge_access",
        "is_quarantined",
        "updated_at",
    ]


@admin.register(DiceRoll)
class DiceRollAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "team",
        "dice_a",
        "dice_b",
        "rolled_number",
        "previous_position",
        "current_position",
        "created_at",
    ]
