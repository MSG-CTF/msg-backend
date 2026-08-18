from django.contrib import admin

from apps.teams.models import Team


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "total_score", "mileage_balance", "board_position", "is_banned"]
