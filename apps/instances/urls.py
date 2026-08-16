from django.urls import path

from apps.instances.views import (
    InstanceCreateView,
    InstanceDeleteView,
    InstanceExtendView,
    InstanceResetView,
)

urlpatterns = [
    path("instances", InstanceCreateView.as_view(), name="instance-create"),
    path("instances/<uuid:instance_id>", InstanceDeleteView.as_view(), name="instance-delete"),
    path("instances/<uuid:instance_id>/reset", InstanceResetView.as_view(), name="instance-reset"),
    path("instances/<uuid:instance_id>/extend", InstanceExtendView.as_view(), name="instance-extend"),
]
