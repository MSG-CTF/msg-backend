from rest_framework import serializers
from .models import Contest

class ContestSerializer(serializers.ModelSerializer):
    status = serializers.ReadOnlyField()
    remaining_time = serializers.ReadOnlyField()
    time_until_start = serializers.ReadOnlyField()

    class Meta:
        model = Contest
        fields = [
            "contest_id",
            "name",
            "status",
            "start_time",
            "end_time",
            "remaining_time",
            "time_until_start",
        ]