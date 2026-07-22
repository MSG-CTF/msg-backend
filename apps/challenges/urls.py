from django.urls import path
from . import views

urlpatterns = [
    # 1. Challenge API (참가자용)
    path('challenges/<int:challenge_id>', views.ChallengeDetailView.as_view(), name='challenge-detail'),

    path('challenges/<int:challenge_id>/submit', views.FlagSubmitView.as_view(), name='flag-submit'),

    path('instances', views.InstanceCreateView.as_view(), name='instance-create'),
    
    path('teams/me/instance', views.TeamInstanceStatusView.as_view(), name='team-instance-status'),
    
    path('instances/<int:instance_id>/reset', views.InstanceResetView.as_view(), name='instance-reset'),
    
    path('instances/<int:instance_id>', views.InstanceDeleteView.as_view(), name='instance-delete'),
    
    path('instances/<int:instance_id>/extend', views.InstanceExtendView.as_view(), name='instance-extend'),


    # 3. Admin Instance API (관리자용)
    path('admin/instances', views.AdminInstanceListView.as_view(), name='admin-instance-list'),
    
    path('admin/instances/<int:instance_id>/reset', views.AdminInstanceResetView.as_view(), name='admin-instance-reset'),
    
    path('admin/instances/<int:instance_id>', views.AdminInstanceDeleteView.as_view(), name='admin-instance-delete'),
]