from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("koth", "0002_kothtokenverificationattempt"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="kothchallenge",
            name="uq_koth_challenge_club_title",
        ),
        migrations.AddConstraint(
            model_name="kothchallenge",
            constraint=models.UniqueConstraint(
                fields=["club"], name="uq_koth_challenge_club"
            ),
        ),
    ]
