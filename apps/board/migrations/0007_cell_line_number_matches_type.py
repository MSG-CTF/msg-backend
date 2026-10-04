from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("board", "0006_cell_line_number")]

    operations = [
        migrations.AddConstraint(
            model_name="cell",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        type="CHALLENGE",
                        line_number__isnull=False,
                        line_number__gte=1,
                        line_number__lte=6,
                    )
                    | (~models.Q(type="CHALLENGE") & models.Q(line_number__isnull=True))
                ),
                name="cell_line_number_matches_type",
            ),
        ),
    ]
