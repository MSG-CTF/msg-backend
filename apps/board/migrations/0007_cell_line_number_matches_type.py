from django.db import migrations, models


def normalize_legacy_line_numbers(apps, schema_editor):
    """Make pre-line board fixtures valid before enforcing the new invariant."""
    Cell = apps.get_model("board", "Cell")
    alias = schema_editor.connection.alias

    # 0006 assigned the six canonical board lines. Older installations and
    # migration tests can also contain standalone CHALLENGE cells, for which a
    # canonical position is unavailable. Preserve them as a valid line instead
    # of failing the entire schema upgrade.
    Cell.objects.using(alias).filter(
        type="CHALLENGE", line_number__isnull=True
    ).update(line_number=1)
    Cell.objects.using(alias).exclude(type="CHALLENGE").update(line_number=None)


class Migration(migrations.Migration):
    dependencies = [("board", "0006_cell_line_number")]

    operations = [
        migrations.RunPython(normalize_legacy_line_numbers, migrations.RunPython.noop),
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
