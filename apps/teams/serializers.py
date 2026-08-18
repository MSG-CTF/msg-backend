from rest_framework import serializers

from apps.teams.models import Team


class TeamSerializer(serializers.ModelSerializer):
    team_id = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = Team
        fields = [
            "team_id",
            "name",
            "total_score",
            "mileage_balance",
            "board_position",
            "is_banned",
        ]
