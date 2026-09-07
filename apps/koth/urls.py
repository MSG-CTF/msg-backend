from django.urls import path

from . import views

urlpatterns = [
    path("koth/clubs", views.clubs),
    path("koth/clubs/<str:club_id>", views.club_detail),
    path("koth/me", views.me),
    path("koth/leaderboard", views.leaderboard),
    path("koth/team_token", views.team_token),
]
