from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("adminpanel", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="adminevent",
            name="message",
            field=models.TextField(),
        ),
    ]
