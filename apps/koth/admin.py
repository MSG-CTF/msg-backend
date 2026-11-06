from django.contrib import admin

from .models import (
    KothChallenge,
    KothClub,
    KothScorePeriod,
    KothSolve,
    KothTeamToken,
    KothTokenKeyRotation,
    KothTokenKeyState,
    KothTokenVerificationAttempt,
    KothUserFileBundle,
)


@admin.register(KothClub)
class KothClubAdmin(admin.ModelAdmin):
    list_display = ("name", "club_id")
    search_fields = ("name",)


@admin.register(KothChallenge)
class KothChallengeAdmin(admin.ModelAdmin):
    list_display = ("title", "club", "status", "open_group", "opened_at", "closed_at")
    list_filter = ("status", "club")
    search_fields = ("title", "repository_slug")


@admin.register(KothUserFileBundle)
class KothUserFileBundleAdmin(admin.ModelAdmin):
    list_display = ("challenge", "registry_revision", "source_sha", "present", "created_at")
    list_filter = ("challenge", "present")
    readonly_fields = tuple(field.name for field in KothUserFileBundle._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(KothSolve)
class KothSolveAdmin(admin.ModelAdmin):
    list_display = ("team", "challenge", "earned_score", "solved_at")
    list_filter = ("challenge",)


@admin.register(KothScorePeriod)
class KothScorePeriodAdmin(admin.ModelAdmin):
    list_display = ("challenge", "period_id", "status", "attempts", "applied_at")
    list_filter = ("status", "challenge")


@admin.register(KothTeamToken)
class KothTeamTokenAdmin(admin.ModelAdmin):
    list_display = ("team", "issued_at", "key_version")
    readonly_fields = tuple(field.name for field in KothTeamToken._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(KothTokenKeyRotation, KothTokenKeyState)
class KothTokenKeyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(KothTokenVerificationAttempt)
class KothTokenVerificationAttemptAdmin(admin.ModelAdmin):
    list_display = ("challenge", "created_at")
    list_filter = ("challenge",)
    readonly_fields = ("challenge", "created_at")
