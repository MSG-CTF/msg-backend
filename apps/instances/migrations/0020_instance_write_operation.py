import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instances", "0019_instance_create_failure_reason")]

    operations = [
        migrations.CreateModel(
            name="InstanceWriteOperation",
            fields=[
                (
                    "operation_id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[("CREATE", "Create"), ("RESET", "Reset")],
                        max_length=10,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("STARTED", "Started"),
                            ("RECOVERY_PENDING", "Recovery Pending"),
                            ("COMPLETED", "Completed"),
                            ("REJECTED", "Rejected"),
                            ("REVIEW_REQUIRED", "Review Required"),
                        ],
                        default="STARTED",
                        max_length=20,
                    ),
                ),
                ("user_id", models.UUIDField()),
                ("team_id", models.UUIDField()),
                ("challenge_id", models.UUIDField()),
                ("release_id", models.UUIDField(blank=True, null=True)),
                ("source_instance_id", models.UUIDField(blank=True, null=True)),
                ("remote_instance_id", models.UUIDField(blank=True, null=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                (
                    "last_error_code",
                    models.CharField(blank=True, default="", max_length=40),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "instance_write_operations",
                "indexes": [
                    models.Index(
                        fields=["status", "created_at"],
                        name="instance_write_recovery_due",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(
                            status__in=[
                                "STARTED",
                                "RECOVERY_PENDING",
                                "REVIEW_REQUIRED",
                            ]
                        ),
                        fields=("user_id",),
                        name="uq_instance_write_pending_user",
                    )
                ],
            },
        ),
    ]
