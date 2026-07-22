from rest_framework import serializers
from .models import Challenge, ChallengeInstance

class ChallengeDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Challenge
        fields = ['id', 'title', 'description', 'category', 'score', 'has_instance', 'created_at']

class ChallengeInstanceSerializer(serializers.ModelSerializer):
    challenge_title = serializers.ReadOnlyField(source='challenge.title')

    class Meta:
        model = ChallengeInstance
        fields = ['id', 'challenge', 'challenge_title', 'connection_url', 'status', 'expires_at', 'created_at']

class FlagSubmitSerializer(serializers.Serializer):
    flag = serializers.CharField(max_length=255, required=True)