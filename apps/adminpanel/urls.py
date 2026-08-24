from django.urls import path

from . import views

urlpatterns = [
    path("admin/teams", views.team_list),
    path("admin/teams/<str:team_id>/ban", views.team_ban),
    path("admin/teams/<str:team_id>/mileage", views.team_mileage),
    path("admin/payment/checkout", views.payment_checkout),
    path("admin/payment/history", views.payment_history),
    path("admin/payment/<str:history_id>/refund", views.payment_refund),
]