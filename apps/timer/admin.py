from django.contrib import admin
from .models import Contest

@admin.register(Contest)
class ContestAdmin(admin.ModelAdmin):
    list_display = ('name', 'startTime', 'endTime', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)
    

