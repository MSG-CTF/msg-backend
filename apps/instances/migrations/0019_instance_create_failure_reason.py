from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instances", "0018_merge_staged_activation_and_publication")]
    operations = [
        migrations.AlterField(
            model_name="instance", name="delete_reason",
            field=models.CharField(blank=True, choices=[
                ("USER_REQUESTED", "User Requested"),
                ("REPLACED_BY_NEW_INSTANCE", "Replaced By New Instance"),
                ("TTL_EXPIRED", "Ttl Expired"),
                ("IDLE_EXPIRED", "Idle Expired"),
                ("HARD_TIMEOUT", "Hard Timeout"),
                ("ADMIN_FORCED", "Admin Forced"),
                ("CREATE_FAILED_CLEANUP", "Create Failed Cleanup"),
            ], max_length=40, null=True),
        ),
    ]
