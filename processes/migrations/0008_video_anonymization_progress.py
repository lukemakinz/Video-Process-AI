from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("processes", "0007_analysissegment_operation_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="video",
            name="anonymization_progress_current",
            field=models.PositiveIntegerField(default=0, verbose_name="przetworzone klatki anonimizacji"),
        ),
        migrations.AddField(
            model_name="video",
            name="anonymization_progress_label",
            field=models.CharField(blank=True, max_length=120, verbose_name="etap anonimizacji"),
        ),
        migrations.AddField(
            model_name="video",
            name="anonymization_progress_percent",
            field=models.PositiveSmallIntegerField(default=0, verbose_name="postęp anonimizacji (%)"),
        ),
        migrations.AddField(
            model_name="video",
            name="anonymization_progress_total",
            field=models.PositiveIntegerField(default=0, verbose_name="liczba klatek anonimizacji"),
        ),
        migrations.AddField(
            model_name="video",
            name="anonymization_progress_updated_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="ostatnia aktualizacja postępu anonimizacji"),
        ),
    ]
