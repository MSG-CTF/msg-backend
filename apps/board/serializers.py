from rest_framework import serializers

from apps.board.models import Cell, Challenge, ChanceCard, DiceRoll, TeamBoardState, TeamChallengeAccess


class CellSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cell
        fields = ["cell_index", "type", "difficulty", "name"]


class ChanceCardSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChanceCard
        fields = ["card_id", "name", "description", "effect", "usage_timing"]


class ChallengeCandidateSerializer(serializers.ModelSerializer):
    challenge_id = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = Challenge
        fields = ["challenge_id", "title", "category", "club_name", "score"]


class ChallengeSerializer(serializers.ModelSerializer):
    challenge_id = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = Challenge
        fields = ["challenge_id", "title", "category", "club_name", "difficulty", "description", "score"]


class TeamChallengeAccessSerializer(serializers.ModelSerializer):
    access_id = serializers.UUIDField(source="id", read_only=True)
    challenge = ChallengeSerializer(read_only=True)
    source_cell_index = serializers.IntegerField(source="source_cell_id", read_only=True)

    class Meta:
        model = TeamChallengeAccess
        fields = ["access_id", "challenge", "source_cell_index", "status", "opened_at", "cleared_at"]


class TeamBoardStateSerializer(serializers.ModelSerializer):
    position = serializers.IntegerField(source="position_id", read_only=True)
    current_cell = CellSerializer(source="position", read_only=True)
    active_challenge = TeamChallengeAccessSerializer(source="active_challenge_access", read_only=True)

    class Meta:
        model = TeamBoardState
        fields = [
            "position",
            "current_cell",
            "dice_rolls_left",
            "active_challenge",
            "is_quarantined",
            "next_dice_reset_at",
            "quarantine_attempts_left",
            "airport_move_used",
            "has_passed_start",
            "updated_at",
        ]


class DiceRollSerializer(serializers.ModelSerializer):
    dice_roll_id = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = DiceRoll
        fields = [
            "dice_roll_id",
            "dice_a",
            "dice_b",
            "rolled_number",
            "previous_position",
            "current_position",
            "created_at",
        ]
