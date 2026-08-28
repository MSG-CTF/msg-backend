from django.contrib import admin
from django.urls import path, include

from apps.common.health import healthz

urlpatterns = [
    path("healthz", healthz),
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.timer.urls")),
    path("api/v1/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.adminpanel.urls")),
    path("api/v1/", include("apps.teams.urls")),
    path("api/v1/", include("apps.ranking.urls")),
    path("api/v1/", include("apps.challenge.urls")),
    path("api/v1/", include("apps.instances.urls")),
]

