from django.contrib import admin
from .models import Challenge, ChallengeInstance

@admin.register(Challenge)
class ChallengeAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'category', 'score', 'has_instance', 'created_at')
    search_fields = ('title', 'category')
    list_filter = ('category', 'has_instance')

@admin.register(ChallengeInstance)
class ChallengeInstanceAdmin(admin.ModelAdmin):
    list_display = ('id', 'challenge', 'user', 'status', 'expires_at', 'created_at')
    list_filter = ('status',)