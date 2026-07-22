from rest_framework import serializers
from .models import Contest

class ContestSerializer(serializers.ModelSerializer):
    status = serializers.ReadOnlyField()
    time_until_start = serializers.ReadOnlyField()
    remaining_seconds = serializers.ReadOnlyField()
    remaining_display = serializers.ReadOnlyField()

    class Meta:
        model = Contest
        fields = [
            'name',
            'status',
            'startTime',
            'endTime',
            'time_until_start',
            'remaining_seconds',
            'remaining_display',
        ]

