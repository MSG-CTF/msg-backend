from django.urls import path

from .views import ranking_list

urlpatterns = [
    path('', ranking_list),
]