import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.accounts.models import Team, User
from apps.instances import tests as instance_tests
from apps.instances.models import Instance, InstanceStatus
from apps.instances.services import SchedulerError

ENDPOINTS = [{
    "container_name": "app", "port": 8080, "protocol": "HTTP",
    "service_url": "https://teammate.example",
}]


@override_settings(
    CACHES=instance_tests.LOCMEM,
    SECURE_SSL_REDIRECT=False,
    SCHEDULER_API_TOKEN="test-scheduler-token",
)
class TeamMemberInstanceTests(TestCase):
    setUp_base = instance_tests.InstanceLockTests.setUp
    auth = instance_tests.InstanceLockTests.auth

    def setUp(self):
        self.setUp_base()
        self.teammate = User.objects.create_user(
            login_id="instance-leader", password="pw1234", nickname="팀장님",
            team=self.team, is_leader=True,
        )

    def teammate_instance(self, **overrides):
        values = {
            "user": self.teammate, "team": self.team, "challenge": self.challenge,
            "release": self.release, "status": InstanceStatus.RUNNING,
        }
        values.update(overrides)
        return Instance.objects.create(**values)

    def scheduler_data(self, instance, **overrides):
        data = {
            "instance_id": str(instance.instance_id),
            "team_id": str(self.team.team_id),
            "challenge_id": str(self.challenge.challenge_id),
            "status": "RUNNING",
            "service_url": ENDPOINTS[0]["service_url"],
            "endpoints": ENDPOINTS,
        }
        data.update(overrides)
        return data

    @patch("apps.instances.views.call_scheduler_active")
    def test_lists_teammate_running_instance_with_owner(self, active):
        instance = self.teammate_instance()
        active.return_value = self.scheduler_data(instance)
        res = self.client.get("/api/v1/teams/me/member_instances")
        self.assertEqual(res.status_code, 200, res.data)
        rows = res.data["data"]["instances"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["nickname"], "팀장님")
        self.assertTrue(rows[0]["is_leader"])
        self.assertEqual(rows[0]["user_id"], str(self.teammate.user_id))
        self.assertEqual(rows[0]["instance"]["challenge_id"], str(self.challenge.challenge_id))
        self.assertEqual(rows[0]["instance"]["challenge_title"], "Web Basic")
        self.assertEqual(rows[0]["instance"]["status"], "RUNNING")
        self.assertEqual(rows[0]["instance"]["endpoints"], ENDPOINTS)
        # 본인 인스턴스는 /teams/me/instances 가 맡으므로 팀원만 조회한다
        self.assertEqual([call.args[0] for call in active.call_args_list], [self.teammate])

    @patch("apps.instances.views.call_scheduler_active")
    def test_teammate_without_active_instance_returns_empty(self, active):
        active.side_effect = SchedulerError("INSTANCE_NOT_FOUND", "없음", 404)
        res = self.client.get("/api/v1/teams/me/member_instances")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["instances"], [])

    @patch("apps.instances.views.call_scheduler_active")
    def test_finished_instance_is_synced_and_hidden(self, active):
        instance = self.teammate_instance()
        active.return_value = self.scheduler_data(instance, status="STOPPED")
        res = self.client.get("/api/v1/teams/me/member_instances")
        self.assertEqual(res.data["data"]["instances"], [])
        instance.refresh_from_db()
        self.assertEqual(instance.status, InstanceStatus.STOPPED)

    @patch("apps.instances.views.call_scheduler_active")
    def test_unrecorded_instance_is_skipped_without_creating_a_row(self, active):
        active.return_value = {
            "instance_id": str(uuid.uuid4()), "team_id": str(self.team.team_id),
            "challenge_id": str(self.challenge.challenge_id), "status": "REQUESTED",
        }
        res = self.client.get("/api/v1/teams/me/member_instances")
        self.assertEqual(res.data["data"]["instances"], [])
        self.assertFalse(Instance.objects.exists())

    @patch("apps.instances.views.call_scheduler_active")
    def test_scope_mismatch_and_scheduler_failure_are_errors(self, active):
        instance = self.teammate_instance()
        active.return_value = self.scheduler_data(instance, team_id=str(uuid.uuid4()))
        res = self.client.get("/api/v1/teams/me/member_instances")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data["code"], "SCHEDULER_UNAVAILABLE")
        active.return_value = None
        active.side_effect = SchedulerError("SCHEDULER_UNAVAILABLE", "연결 실패", 503)
        res = self.client.get("/api/v1/teams/me/member_instances")
        self.assertEqual(res.status_code, 503)

    @patch("apps.instances.views.call_scheduler_active")
    def test_other_team_members_are_not_listed(self, active):
        other = Team.objects.create(team_name="다른팀")
        User.objects.create_user(login_id="other-member", password="pw1234", nickname="남", team=other)
        active.side_effect = SchedulerError("INSTANCE_NOT_FOUND", "없음", 404)
        self.client.get("/api/v1/teams/me/member_instances")
        self.assertEqual([call.args[0] for call in active.call_args_list], [self.teammate])

    def test_user_without_team_is_rejected(self):
        User.objects.filter(pk=self.user.pk).update(team=None)
        res = self.client.get("/api/v1/teams/me/member_instances")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "USER_HAS_NO_TEAM")
