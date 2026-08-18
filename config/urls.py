"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from apps.board.views import BoardView, DashboardView, DebugSolveActiveChallengeView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.adminpanel.urls")),
    path("api/v1/", include("apps.teams.urls")),
    path("api/v1/board", BoardView.as_view(), name="board"),
    path("api/v1/board/", include("apps.board.urls")),
    path("", include("apps.koth.urls")),
    # /api/v1 명세 밖, 로컬 프리뷰 전용 (DEBUG=True에서만 응답).
    path("board/_debug/solve", DebugSolveActiveChallengeView.as_view(), name="board-debug-solve"),
]
