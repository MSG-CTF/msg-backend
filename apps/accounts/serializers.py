from rest_framework import serializers


class LoginSerializer(serializers.Serializer):
    login_id = serializers.CharField()
    password = serializers.CharField()


class RefreshTokenSerializer(serializers.Serializer):
    refresh_token = serializers.CharField()