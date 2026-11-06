from django.db import migrations, models
from django.db.models.functions import Now


# 기존 마이그레이션의 규칙은 실행 시점의 모듈과 분리해 고정한다
QA_PATTERN = r"\A[Qq][Aa][0-9]+-[A-Za-z][A-Za-z0-9_-]*-[0-9A-Fa-f]{8}-[0-9]+\Z"

CREATE_TRIGGERS = r"""
CREATE FUNCTION accounts_register_generated_qa_team() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $qa_team$
BEGIN
    INSERT INTO public.test_team_identities (team_id, registration_source)
    VALUES (NEW.team_id, 'QA_NAMESPACE') ON CONFLICT (team_id) DO NOTHING;
    RETURN NEW;
END;
$qa_team$;
CREATE TRIGGER teams_register_generated_qa
AFTER INSERT OR UPDATE OF team_name ON teams
FOR EACH ROW WHEN (NEW.team_name ~ '\A[Qq][Aa][0-9]+-[A-Za-z][A-Za-z0-9_-]*-[0-9A-Fa-f]{8}-[0-9]+\Z')
EXECUTE FUNCTION accounts_register_generated_qa_team();

CREATE FUNCTION accounts_preserve_preview_test_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $preview_team$
BEGIN
    INSERT INTO public.test_team_identities (team_id, registration_source)
    VALUES (NEW.preview_team_id, 'LEGACY_PREVIEW') ON CONFLICT (team_id) DO NOTHING;
    RETURN NEW;
END;
$preview_team$;
CREATE TRIGGER preview_tokens_preserve_test_identity
AFTER INSERT OR UPDATE OF preview_team_id ON koth_preview_tokens
FOR EACH ROW EXECUTE FUNCTION accounts_preserve_preview_test_identity();
"""

DROP_TRIGGERS = """
DROP TRIGGER preview_tokens_preserve_test_identity ON koth_preview_tokens;
DROP FUNCTION accounts_preserve_preview_test_identity();
DROP TRIGGER teams_register_generated_qa ON teams;
DROP FUNCTION accounts_register_generated_qa_team();
"""


def preserve_existing_test_classification(apps, schema_editor):
    team = apps.get_model("accounts", "Team")
    preview = apps.get_model("koth", "KothPreviewToken")
    identity = apps.get_model("accounts", "TestTeamIdentity")
    alias = schema_editor.connection.alias
    for queryset, source in [
        (
            preview.objects.using(alias)
            .values_list("preview_team_id", flat=True)
            .distinct(),
            "LEGACY_PREVIEW",
        ),
        (
            team.objects.using(alias)
            .filter(team_name__regex=QA_PATTERN)
            .values_list("pk", flat=True),
            "QA_NAMESPACE",
        ),
    ]:
        identity.objects.using(alias).bulk_create(
            [
                identity(team_id=team_id, registration_source=source)
                for team_id in queryset
            ],
            batch_size=500,
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_team_koth_division"),
        ("koth", "0014_kothpreviewtoken_key_version_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="TestTeamIdentity",
            fields=[
                ("team_id", models.UUIDField(primary_key=True, serialize=False)),
                (
                    "registration_source",
                    models.CharField(
                        choices=[
                            ("EXPLICIT_QA", "EXPLICIT_QA"),
                            ("QA_NAMESPACE", "QA_NAMESPACE"),
                            ("LEGACY_PREVIEW", "LEGACY_PREVIEW"),
                        ],
                        default="EXPLICIT_QA",
                        db_default="EXPLICIT_QA",
                        max_length=20,
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, db_default=Now()),
                ),
            ],
            options={"db_table": "test_team_identities"},
        ),
        migrations.RunSQL(CREATE_TRIGGERS, DROP_TRIGGERS),
        migrations.RunPython(
            preserve_existing_test_classification, migrations.RunPython.noop
        ),
    ]
