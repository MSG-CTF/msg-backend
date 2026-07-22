from django.urls import path
from .views import MyTeamView

urlpatterns = [
    path('me', MyTeamView.as_view(), name='my-team'),
]