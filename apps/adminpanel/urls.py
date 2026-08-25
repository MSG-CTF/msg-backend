from django.urls import path

from . import views

urlpatterns = [
    path("admin/teams", views.team_list),
    path("admin/teams/<str:team_id>/ban", views.team_ban),
    path("admin/teams/<str:team_id>/mileage", views.team_mileage),
    path("admin/instances", views.instance_list),
    path("admin/instances/<uuid:instance_id>", views.instance_force_delete),
]