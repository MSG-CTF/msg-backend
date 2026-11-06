import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instances", "0022_automatic_publication")]
    operations = [
        migrations.CreateModel(name="ServiceKeyState", fields=[
            ("purpose", models.CharField(max_length=32, primary_key=True, serialize=False)),
            ("active_key_id", models.CharField(max_length=64)),
            ("generation", models.PositiveIntegerField(default=1)),
            ("activated_at", models.DateTimeField()),
        ], options={"db_table": "service_key_states"}),
        migrations.CreateModel(name="ServiceKeyRotation", fields=[
            ("rotation_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("purpose", models.CharField(max_length=32)),
            ("previous_key_id", models.CharField(blank=True, max_length=64)),
            ("active_key_id", models.CharField(max_length=64)),
            ("generation", models.PositiveIntegerField()),
            ("reencrypted_rows", models.PositiveIntegerField(default=0)),
            ("actor", models.CharField(max_length=100)),
            ("reason", models.CharField(max_length=250)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
        ], options={"db_table": "service_key_rotations", "ordering": ["-created_at"]}),
    ]
