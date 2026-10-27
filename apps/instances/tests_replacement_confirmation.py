import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.instances.models import DeleteReason, Instance, InstanceStatus
from apps.instances.services import SchedulerError, scheduler_delete_reason
from apps.instances.tests import InstanceLockTests, LOCMEM


@override_settings(CACHES=LOCMEM, SECURE_SSL_REDIRECT=False, SCHEDULER_API_TOKEN="test-scheduler-token")
class ReplacementConfirmationTests(TestCase):
    setUp = InstanceLockTests.setUp
    auth = InstanceLockTests.auth

    @patch("apps.instances.views.call_scheduler_create")
    def test_participant_create_always_requires_confirmation_in_scheduler(self, create):
        create.return_value = {"instance_id": str(uuid.uuid4()), "status": "REQUESTED"}
        response = self.client.post("/api/v1/instances", {"challenge_id": str(self.challenge.pk)}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(create.call_args.kwargs, {
            "require_replacement_confirmation": True, "replace_instance_id": None,
        })

    @patch("apps.instances.services.scheduler_request")
    def test_confirmed_id_reaches_scheduler_without_changing_ownership(self, request):
        replacement = str(uuid.uuid4())
        request.return_value = {"instance_id": str(uuid.uuid4()), "status": "REQUESTED"}
        response = self.client.post("/api/v1/instances", {
            "challenge_id": str(self.challenge.pk), "replace_instance_id": replacement,
        }, format="json")
        self.assertEqual(response.status_code, 202)
        body = request.call_args.kwargs["body"]
        self.assertTrue(body["require_replacement_confirmation"])
        self.assertEqual(body["replace_instance_id"], replacement)
        self.assertEqual(body["user_id"], str(self.user.pk))
        self.assertEqual(body["team_id"], str(self.team.pk))

    @patch("apps.instances.views.call_scheduler_create")
    def test_unconfirmed_or_stale_request_preserves_existing_record(self, create):
        previous = Instance.objects.create(user=self.user, team=self.team, challenge=self.challenge,
                                           release=self.release, status=InstanceStatus.RUNNING)
        for code in ["INSTANCE_REPLACEMENT_REQUIRED", "INSTANCE_STATE_CHANGED"]:
            create.side_effect = SchedulerError(code, "현재 상태를 확인해주세요", 409)
            response = self.client.post("/api/v1/instances", {"challenge_id": str(self.challenge.pk)}, format="json")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["code"], code)
            previous.refresh_from_db()
            self.assertEqual(previous.status, InstanceStatus.RUNNING)
            self.assertIsNone(previous.delete_reason)
            self.assertEqual(Instance.objects.count(), 1)

    @patch("apps.instances.views.call_scheduler_create")
    def test_invalid_confirmation_or_client_opt_out_is_rejected(self, create):
        for extra in [
            {"replace_instance_id": "wrong"}, {"replace_instance_id": 1},
            {"replace_instance_id": str(uuid.UUID(int=0))},
            {"require_replacement_confirmation": False},
        ]:
            response = self.client.post("/api/v1/instances", {"challenge_id": str(self.challenge.pk), **extra}, format="json")
            self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    @patch("apps.instances.views.call_scheduler_active", side_effect=SchedulerError("INSTANCE_NOT_FOUND", "absent", 404))
    @patch("apps.instances.views.call_scheduler_detail")
    def test_failed_create_reason_survives_cleanup_and_history(self, detail, active):
        previous = Instance.objects.create(user=self.user, team=self.team, challenge=self.challenge,
                                           release=self.release, status=InstanceStatus.REQUESTED)
        detail.return_value = {"instance_id": str(previous.pk), "status": "CLEANED",
                               "delete_reason": "CREATE_FAILED_CLEANUP", "release_id": str(self.release.pk)}
        response = self.client.get("/api/v1/teams/me/instances", {"challenge_id": str(self.challenge.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["delete_reason"], "CREATE_FAILED_CLEANUP")
        previous.refresh_from_db()
        self.assertEqual(previous.delete_reason, DeleteReason.CREATE_FAILED_CLEANUP)
        self.assertEqual(previous.status, InstanceStatus.CLEANED)
        self.assertEqual(scheduler_delete_reason({"status": "CLEANED", "delete_reason": "USER_REQUESTED"}), DeleteReason.USER_REQUESTED)
