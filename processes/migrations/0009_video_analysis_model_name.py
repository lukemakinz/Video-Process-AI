from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("processes", "0008_video_anonymization_progress"),
    ]

    operations = [
        migrations.AddField(
            model_name="video",
            name="analysis_model_name",
            field=models.CharField(blank=True, max_length=120, verbose_name="model analizy Gemini"),
        ),
    ]

