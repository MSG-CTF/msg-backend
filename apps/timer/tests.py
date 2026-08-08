from django.test import TestCase
from datetime import timedelta
from django.utils import timezone
from rest_framework.test import APITestCase
from apps.timer.models import Contest

class TimerAPITest(APITestCase):
    def test_running_when_contest_in_progress(self):
        now = timezone.now()
        Contest.objects.create(
            name="test",
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=11),
            is_active=True,
        )

        response = self.client.get("/api/v1/timer")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "SUCCESS")
        self.assertEqual(response.data["data"]["status"], "RUNNING")

    def test_before_when_contest_not_started(self):
        now = timezone.now()
        Contest.objects.create(
            name="test",
            start_time=now + timedelta(hours=1),
            end_time=now + timedelta(hours=13),
            is_active=True,
        )

        response = self.client.get("/api/v1/timer")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], "BEFORE")

    def test_ended_when_contest_finished(self):
        now = timezone.now()
        Contest.objects.create(
            name="test",
            start_time=now - timedelta(hours=13),
            end_time=now - timedelta(hours=1),
            is_active=True,
        )

        response = self.client.get("/api/v1/timer")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], "ENDED")

    def test_null_data_when_no_active_contest(self):
        response = self.client.get("/api/v1/timer")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "SUCCESS")
        self.assertIsNone(response.data["data"])
