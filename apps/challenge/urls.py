from django.urls import path

from apps.challenge.views import ChallengeDetailView, ChallengeSubmitView

urlpatterns = [
    path("<uuid:challenge_id>", ChallengeDetailView.as_view(), name="challenge-detail"),
    path("<uuid:challenge_id>/submit", ChallengeSubmitView.as_view(), name="challenge-submit"),
]
