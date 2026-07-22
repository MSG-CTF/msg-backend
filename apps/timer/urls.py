from django.urls import path
from .views import ping, contest_timer

urlpatterns = [
    path('ping/', ping),
    path('', contest_timer),
    
]