from django.contrib import admin
from django.urls import path, include
from django.conf import settings

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/auth/', include('apps.accounts.urls')),
    path('api/v1/ranking/', include('apps.ranking.urls')),
]

if settings.DEBUG:
    urlpatterns += [
        path('devtools/', include('apps.common.urls')),
    ]