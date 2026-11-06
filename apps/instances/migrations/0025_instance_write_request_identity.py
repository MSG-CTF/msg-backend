from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instances", "0023_service_key_lifecycle")]
    operations = [
        migrations.AddField(
            model_name="instancewriteoperation",
            name="request_id_used",
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name="instancewriteoperation",
            index=models.Index(fields=["status", "updated_at"], condition=models.Q(request_id_used=True), name="instance_req_recovery_due"),
        ),
        migrations.AddIndex(
            model_name="instancewriteoperation",
            index=models.Index(fields=["user_id", "team_id", "-created_at", "-operation_id"], condition=models.Q(request_id_used=True), name="instance_req_owner_latest"),
        ),
    ]
