from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="team",
            name="team_score",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=20),
        ),
    ]
