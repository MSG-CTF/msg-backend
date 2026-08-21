from django.urls import path
from .views import team_ranking


urlpatterns = [
    path("ranking", team_ranking),     
]