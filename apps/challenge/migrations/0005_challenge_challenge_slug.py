from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("challenge", "0004_alter_challenge_decay_alter_challenge_minimum_score"),
    ]

    operations = [
        migrations.AddField(
            model_name="challenge",
            name="challenge_slug",
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
    ]
