from django.urls import path

from apps.teams.views import TeamMeView

app_name = "teams"

urlpatterns = [
    path("me", TeamMeView.as_view(), name="team-me"),
]
