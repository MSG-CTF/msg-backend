from rest_framework import serializers
from apps.teams.models import Team

class RankingSerializer(serializers.ModelSerializer):
    rank = serializers.IntegerField(read_only=True)

    class Meta:
        model = Team
        fields = ['rank', 'name', 'team_score', 'mileage', ]
        