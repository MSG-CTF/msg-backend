from django.contrib.auth.models import AbstractUser
from django.db import models

from apps.teams.models import Team

# Create your models here.

class User(AbstractUser):
    ROLE_CHOICES = [
        ('PARTICIPANT', 'Participant'),
        ('ADMIN', 'Admin')
    ]

    nickname = models.CharField(max_length=50)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='PARTICIPANT')
    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='members',
    )
    score = models.IntegerField(default=0)

    def __str__(self):
        return self.username