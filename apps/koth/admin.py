from django.contrib import admin

from .models import KothChallenge, KothClub, KothScoringPeriod, KothSolve, KothTeamToken


@admin.register(KothClub)
class KothClubAdmin(admin.ModelAdmin):
    list_display = ["club_id", "name"]


@admin.register(KothChallenge)
class KothChallengeAdmin(admin.ModelAdmin):
    list_display = [
        "koth_challenge_id", "title", "club", "category", "open_group", "status",
        "opened_at", "closed_at", "score_api_base_url",
    ]
    list_filter = ["status", "category"]


@admin.register(KothTeamToken)
class KothTeamTokenAdmin(admin.ModelAdmin):
    list_display = ["team", "issued_at"]


@admin.register(KothSolve)
class KothSolveAdmin(admin.ModelAdmin):
    list_display = ["id", "team", "challenge", "earned_score", "solved_at"]


@admin.register(KothScoringPeriod)
class KothScoringPeriodAdmin(admin.ModelAdmin):
    list_display = ["id", "challenge", "period_id", "processed_at"]
