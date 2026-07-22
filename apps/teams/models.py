from django.db import models

# Create your models here.

class Team(models.Model):
    name = models.CharField(max_length=50, unique=True)
    team_score = models.IntegerField(default=0)
    mileage = models.IntegerField(default=0)
    position = models.IntegerField(default=0)

    is_banned = models.BooleanField(default=False)
    ban_reason = models.CharField(max_length=255, null=True, blank=True)
    banned_at = models.DateTimeField(null=True, blank=True)
    banned_by = models.CharField(max_length=50, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name