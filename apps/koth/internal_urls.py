from django.urls import path

from . import views

urlpatterns = [
    path("internal/koth/team_tokens/verify", views.verify_team_token),
    path("internal/teams", views.internal_teams),
]
