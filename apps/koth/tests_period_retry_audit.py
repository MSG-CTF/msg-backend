import uuid
from datetime import datetime, timedelta, timezone as tz
from urllib.parse import quote
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from apps.adminpanel.models import AdminEvent
from apps.koth.models import (
    KothChallenge,
    KothChallengeStatus,
    KothClub,
    KothScorePeriod,
)

SimpleRateThrottle.allow_request = lambda self, request, view: True
User = get_user_model()


class PeriodRetryAuditTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            login_id="period-retry-admin",
            password="Audit-Strong#Pw-2026",
            nickname="admin",
            role="ADMIN",
        )
        club = KothClub.objects.create(name="retry-club")
        self.challenge = KothChallenge.objects.create(
            club=club,
            title="retry-ch",
            open_group=1,
            status=KothChallengeStatus.ACTIVE,
            inbound_internal_token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        )
        # 15분 경계 + 1시간 전
        self.period = datetime(2026, 1, 1, 0, 0, 0, tzinfo=tz.utc)
        self._period_str = quote(self.period.isoformat().replace('+00:00', '+00:00'), safe='')
        self.row = KothScorePeriod.objects.create(
            challenge=self.challenge,
            period_id=self.period,
            status="FAILED",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_audit_event_written_when_retry_completes(self):
        before = AdminEvent.objects.count()
        with patch("apps.koth.admin_views.services.poll_challenge_period"):
            response = self.client.post(
                f"/api/v1/admin/koth/challenges/{self.challenge.pk}/periods/"
                f"{self._period_str}/retry",
                {},
                format="json",
                HTTP_HOST="localhost",
                secure=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AdminEvent.objects.count(), before + 1)
        event = AdminEvent.objects.latest("pk")
        self.assertEqual(event.type, AdminEvent.EventType.KOTH_PERIOD_RETRIED)
        self.assertEqual(event.severity, AdminEvent.Severity.WARNING)
        self.assertIn(str(self.challenge.pk), event.message)

    def test_audit_event_not_written_when_period_already_applied(self):
        self.row.status = "APPLIED"
        self.row.save()
        before = AdminEvent.objects.count()
        response = self.client.post(
            f"/api/v1/admin/koth/challenges/{self.challenge.pk}/periods/"
            f"{self._period_str}/retry",
            {},
            format="json",
            HTTP_HOST="localhost",
            secure=True,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(AdminEvent.objects.count(), before)
