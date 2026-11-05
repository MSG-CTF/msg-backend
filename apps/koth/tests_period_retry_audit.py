import uuid
from datetime import datetime, timezone as tz
from urllib.parse import quote
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.adminpanel.models import AdminEvent
from apps.koth.models import (
    KothChallenge,
    KothChallengeStatus,
    KothClub,
    KothScorePeriod,
)

User = get_user_model()


@override_settings(
    REST_FRAMEWORK={
        "DEFAULT_THROTTLE_CLASSES": [],
        "DEFAULT_THROTTLE_RATES": {},
    }
)
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
        # 15분 경계 + 과거(이미 끝난 구간)
        self.period = datetime(2026, 1, 1, 0, 0, 0, tzinfo=tz.utc)
        self._period_str = quote(self.period.isoformat(), safe="")
        self.row = KothScorePeriod.objects.create(
            challenge=self.challenge,
            period_id=self.period,
            status="FAILED",
        )
        self.client = APIClient(raise_request_exception=False)
        self.client.force_authenticate(user=self.admin)

    def _retry(self):
        return self.client.post(
            f"/api/v1/admin/koth/challenges/{self.challenge.pk}/periods/"
            f"{self._period_str}/retry",
            {},
            format="json",
            secure=True,
        )

    def test_audit_event_written_when_retry_completes(self):
        before = AdminEvent.objects.count()
        with patch("apps.koth.admin_views.services.poll_challenge_period"):
            response = self._retry()
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
        response = self._retry()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(AdminEvent.objects.count(), before)

    def test_score_change_rolls_back_when_audit_write_fails(self):
        """감사 저장이 실패하면 점수 반영도 함께 롤백돼야 한다."""

        def apply_score(challenge, period):
            row = KothScorePeriod.objects.filter(
                challenge=challenge, period_id=period
            ).first()
            if row is not None:
                row.status = "APPLIED"
                row.save(update_fields=["status"])

        with patch(
            "apps.koth.admin_views.services.poll_challenge_period",
            side_effect=apply_score,
        ), patch(
            "apps.koth.admin_views._record_event",
            side_effect=DatabaseError("audit write failed"),
        ):
            response = self._retry()
        self.assertEqual(response.status_code, 500)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "FAILED")
        self.assertEqual(
            AdminEvent.objects.filter(
                type=AdminEvent.EventType.KOTH_PERIOD_RETRIED
            ).count(),
            0,
        )
