from rest_framework import serializers

from apps.challenge.models import Challenge


class ChallengeCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    category = serializers.ChoiceField(choices=Challenge.CategoryType.choices)
    difficulty = serializers.ChoiceField(choices=Challenge.DifficultyType.choices)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    flag = serializers.CharField(trim_whitespace=False, write_only=True)
    initial_score = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        default=1000,
    )
    minimum_score = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        default=600,
    )
    decay = serializers.IntegerField(min_value=1, default=70)

    def validate(self, attrs):
        unknown_fields = set(self.initial_data) - set(self.fields)
        if unknown_fields:
            raise serializers.ValidationError(
                f"정의되지 않은 필드입니다: {', '.join(sorted(unknown_fields))}"
            )
        if attrs["initial_score"] < attrs["minimum_score"]:
            raise serializers.ValidationError(
                "initial_score는 minimum_score 이상이어야 합니다"
            )
        return attrs
