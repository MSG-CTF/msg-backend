from django.db import migrations, models


def clear_special_cell_line_numbers(apps, schema_editor):
    """Discard legacy line values from non-challenge cells before validation."""
    Cell = apps.get_model("board", "Cell")
    alias = schema_editor.connection.alias

    # Only canonical board challenge cells have a line. Standalone challenge
    # cells are valid without one, but special cells must never carry one.
    Cell.objects.using(alias).exclude(type="CHALLENGE").update(line_number=None)


class Migration(migrations.Migration):
    dependencies = [("board", "0006_cell_line_number")]

    operations = [
        migrations.RunPython(clear_special_cell_line_numbers, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="cell",
            constraint=models.CheckConstraint(
                condition=models.Q(type="CHALLENGE") | models.Q(line_number__isnull=True),
                name="cell_line_number_requires_challenge",
            ),
        ),
    ]
