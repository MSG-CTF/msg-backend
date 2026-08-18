from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/koth/clubs", views.KothClubsView.as_view(), name="koth-clubs"),
    path("api/v1/koth/clubs/<str:club_id>", views.KothClubDetailView.as_view(), name="koth-club-detail"),
    path("api/v1/koth/me", views.KothMeView.as_view(), name="koth-me"),
    path("api/v1/koth/leaderboard", views.KothLeaderboardView.as_view(), name="koth-leaderboard"),
    path("api/v1/koth/team_token", views.KothTeamTokenView.as_view(), name="koth-team-token"),
    path("internal/teams", views.InternalTeamsView.as_view(), name="internal-teams"),
    # 노션 명세는 team_tokens(언더스코어)지만, 이미 출제자에게 배포된 koth-template은
    # team-tokens(하이픈)를 쓴다. 두 표기 모두 같은 뷰로 받는다.
    path(
        "internal/koth/team_tokens/verify",
        views.InternalTeamTokenVerifyView.as_view(),
        name="internal-team-tokens-verify",
    ),
    path(
        "internal/koth/team-tokens/verify",
        views.InternalTeamTokenVerifyView.as_view(),
        name="internal-team-tokens-verify-hyphen",
    ),
]
