from django.db import migrations, models


def dependency_hell_uses_end_boundary(apps, schema_editor):
    challenge = apps.get_model("koth", "KothChallenge")
    challenge.objects.using(schema_editor.connection.alias).filter(
        repository_slug="koth-dependency-hell"
    ).update(score_period_anchor="END")


class Migration(migrations.Migration):
    dependencies = [("koth", "0010_kothscoreperiod_award_snapshot")]

    operations = [
        migrations.AddField(
            model_name="kothchallenge",
            name="score_period_anchor",
            field=models.CharField(
                choices=[("START", "구간 시작"), ("END", "구간 종료")],
                default="START",
                help_text="문제 서버가 period_id로 받는 시각. 플랫폼 이력은 구간 시작 시각을 유지합니다.",
                max_length=5,
            ),
        ),
        migrations.RunPython(dependency_hell_uses_end_boundary, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="kothchallenge",
            constraint=models.CheckConstraint(
                condition=models.Q(score_period_anchor__in=["START", "END"]),
                name="ck_koth_score_period_anchor",
            ),
        ),
    ]
