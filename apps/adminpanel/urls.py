from django.urls import path

from . import views

urlpatterns = [
    path("admin/teams", views.team_list),
]