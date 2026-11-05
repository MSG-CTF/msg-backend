from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instances", "0023_service_key_lifecycle")]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="scheduler_status",
            field=models.CharField(
                blank=True,
                null=True,
                max_length=30,
                choices=[
                    ("REQUESTED", "Requested"),
                    ("SCHEDULING", "Scheduling"),
                    ("PROVISIONING", "Provisioning"),
                    ("RUNNING", "Running"),
                    ("RESTARTING", "Restarting"),
                    ("RESETTING", "Resetting"),
                    ("STOPPING", "Stopping"),
                    ("STOPPED", "Stopped"),
                    ("FAILED", "Failed"),
                    ("EXPIRED", "Expired"),
                    ("CLEANUP_PENDING", "Cleanup Pending"),
                    ("CLEANED", "Cleaned"),
                ],
            ),
        ),
        migrations.AddField(
            model_name="instance",
            name="scheduler_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="instance",
            name="scheduler_error_code",
            field=models.CharField(blank=True, null=True, max_length=40),
        ),
    ]
