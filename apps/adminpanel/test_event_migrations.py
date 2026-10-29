from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class AdminEventMergeMigrationTests(TransactionTestCase):
    target = ("adminpanel", "0014_admin_event_signature_visibility_solved")
    event_types = {
        "CHALLENGE_RELEASE_APPROVED",
        "CHALLENGE_DELETED",
        "CHALLENGE_FLAG_REVEALED",
        "SIGNATURE_VISIBILITY_CHANGED",
        "SIGNATURE_SOLVED",
    }

    def setUp(self):
        self.latest_targets = MigrationExecutor(connection).loader.graph.leaf_nodes()

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.latest_targets)
        super().tearDown()

    def assert_upgrade_preserves_events(self, source, existing_types):
        executor = MigrationExecutor(connection)
        executor.migrate([source])
        old_model = executor.loader.project_state([source]).apps.get_model(
            "adminpanel", "AdminEvent"
        )
        ids = [
            old_model.objects.create(
                type=kind, actor="migration-qa", message=f"existing {kind}"
            ).pk
            for kind in existing_types
        ]

        executor = MigrationExecutor(connection)
        self.assertFalse(executor.loader.detect_conflicts())
        executor.migrate([self.target])
        model = executor.loader.project_state([self.target]).apps.get_model(
            "adminpanel", "AdminEvent"
        )
        self.assertTrue(
            self.event_types <= set(dict(model._meta.get_field("type").choices))
        )
        self.assertCountEqual(
            model.objects.filter(pk__in=ids).values_list("type", flat=True),
            existing_types,
        )
        for kind in self.event_types:
            event = model(type=kind, actor="migration-qa", message=f"new {kind}")
            event.full_clean()
            event.save()

    def test_upgrade_from_shared_parent(self):
        self.assert_upgrade_preserves_events(
            ("adminpanel", "0011_merge_billing_and_event_types"),
            ["SETTINGS_CHANGED"],
        )

    def test_upgrade_after_release_approval_was_deployed(self):
        self.assert_upgrade_preserves_events(
            ("adminpanel", "0012_admin_event_release_approved"),
            ["CHALLENGE_RELEASE_APPROVED"],
        )

    def test_upgrade_after_delete_and_reveal_were_deployed(self):
        self.assert_upgrade_preserves_events(
            ("adminpanel", "0012_admin_event_challenge_delete_reveal"),
            ["CHALLENGE_DELETED", "CHALLENGE_FLAG_REVEALED"],
        )

    def test_upgrade_after_merged_event_types_were_deployed(self):
        self.assert_upgrade_preserves_events(
            ("adminpanel", "0013_merge_release_and_challenge_events"),
            ["SIGNATURE_CREATED", "SIGNATURE_UPDATED", "SIGNATURE_DELETED"],
        )
