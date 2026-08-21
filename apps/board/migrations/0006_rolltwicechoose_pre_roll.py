from django.db import migrations


def set_pre_roll(apps, schema_editor):
    ChanceCard = apps.get_model("board", "ChanceCard")
    ChanceCard.objects.filter(card_id="card_roll_twice_choose").update(
        usage_timing="PRE_ROLL",
        description="주사위를 굴리기 전에 두 번 굴린 뒤 한 값을 골라 이동합니다.",
    )


def set_post_roll(apps, schema_editor):
    ChanceCard = apps.get_model("board", "ChanceCard")
    ChanceCard.objects.filter(card_id="card_roll_twice_choose").update(
        usage_timing="POST_ROLL",
        description="방금 굴린 값과 한 번 더 굴린 값 중 하나를 골라 이동합니다.",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("board", "0005_quarantineescapecode"),
    ]

    operations = [
        migrations.RunPython(set_pre_roll, set_post_roll),
    ]
