from django.urls import path
from .views import ApiTestView

urlpatterns = [
    path('', ApiTestView.as_view(), name='api-test'),
]