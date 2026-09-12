from django.urls import path

from . import admin_views


urlpatterns = [
    path("admin/signatures", admin_views.signature_collection),
    path("admin/signatures/<uuid:signature_id>", admin_views.signature_detail),
    path(
        "admin/signatures/<uuid:signature_id>/publish",
        admin_views.signature_publish,
    ),
]
