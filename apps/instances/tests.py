import datetime
import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag
from apps.instances.models import (
    ArchitectureType,
    ChallengeRelease,
    ChallengeRuntimeConfig,
    InstanceLock,
    IsolationProfile,
    ReleaseContainer,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(
    CACHES=LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="test-scheduler-token",
)
class InstanceLockTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="인스턴스팀")
        self.user = User.objects.create_user(
            login_id="instance-user",
            password="pw1234",
            nickname="인스턴스유저",
            team=self.team,
        )
        self.challenge = Challenge.objects.create(
            title="Web Basic",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=500,
            description="인스턴스 테스트 문제",
            flag_hash=hash_flag("MSG{flag}"),
            is_published=True,
        )
        self.release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            revision=1,
            architecture=ArchitectureType.AMD64,
            isolation_profile=IsolationProfile.WEB,
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
            is_active=True,
        )

        ReleaseContainer.objects.create(
            release=self.release,
            name="web",
            image="ghcr.io/msg-ctf/challenges/web-basic/web@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            ports=[8080],
            expose=True,
        )

        ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge,
            current_release=self.release,
        )
        self.auth()

    def auth(self):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "instance-user", "password": "pw1234"},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}")

    @patch("apps.instances.views.call_scheduler_create")
    def test_instance_create_creates_user_lock(self, call_scheduler_create):
        # 인스턴스 생성 요청은 사용자 단위 잠금 row를 만든 뒤 처리된다
        instance_id = uuid.uuid4()
        call_scheduler_create.return_value = {
            "instance_id": str(instance_id),
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.user_id),
            "challenge_id": str(self.challenge.challenge_id),
            "status": "REQUESTED",
            "service_url": None,
            "expires_at": (timezone.now() + datetime.timedelta(minutes=120)).isoformat(),
            "hard_expires_at": (timezone.now() + datetime.timedelta(minutes=180)).isoformat(),
            "replaced_instance_id": None,
        }

        res = self.client.post(
            "/api/v1/instances",
            {"challenge_id": str(self.challenge.challenge_id)},
            format="json",
        )

        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertTrue(InstanceLock.objects.filter(user=self.user).exists())
