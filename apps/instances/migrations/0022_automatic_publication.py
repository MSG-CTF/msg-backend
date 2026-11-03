from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("instances", "0021_challenge_user_file_selection")]
    operations = [migrations.CreateModel(
        name="AutomaticPublication",
        fields=[
            ("release", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name="automatic_publication", serialize=False, to="instances.challengerelease")),
            ("artifact_id", models.BigIntegerField(unique=True)),
            ("run_id", models.BigIntegerField()),
            ("source_sha", models.CharField(max_length=40)),
            ("expected_release_id", models.UUIDField(blank=True, null=True)),
            ("expected_generation", models.PositiveBigIntegerField()),
            ("state", models.CharField(default="PENDING", max_length=16)),
            ("error_code", models.CharField(blank=True, default="", max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("preparation", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="instances.imagepreparationrequest")),
        ], options={"db_table": "automatic_publications", "indexes": [models.Index(fields=["state", "created_at"], name="automatic_publications_due")]},
    )]
