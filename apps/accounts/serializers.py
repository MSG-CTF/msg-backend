from rest_framework import serializers


class LoginRequestSerializer(serializers.Serializer):
    loginId = serializers.CharField()
    password = serializers.CharField(write_only=True)