from django.contrib import admin
from .models import RefreshToken, Team, User

# Register your models here.

@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("team_name", "team_score", "mileage", "is_banned")
    search_fields = ("team_name",)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("login_id", "nickname", "team", "role", "is_leader")
    list_filter = ("role", "is_leader")
    search_fields = ("login_id", "nickname")


@admin.register(RefreshToken)
class RefreshTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "expires_at", "created_at")