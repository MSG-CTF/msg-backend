import uuid

from django.db import models


DEFAULT_TEAM_NAME = "test-team"


class Team(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=50, unique=True)
    total_score = models.IntegerField(default=0)
    mileage_balance = models.IntegerField(default=0)
    board_position = models.PositiveSmallIntegerField(default=1)
    is_banned = models.BooleanField(default=False)
    ban_reason = models.CharField(max_length=255, null=True, blank=True)
    banned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teams"
        ordering = ["id"]

    def __str__(self):
        return self.name


def get_default_team():
    team, _ = Team.objects.get_or_create(name=DEFAULT_TEAM_NAME)
    return team
