from django.db import migrations


def backfill_activity_order(apps, schema_editor):
    Activity = apps.get_model("processes", "Activity")
    Operation = apps.get_model("processes", "Operation")
    for operation in Operation.objects.all():
        activities = Activity.objects.filter(operation=operation).order_by("created_at", "id")
        for position, activity in enumerate(activities, start=1):
            if activity.order != position:
                activity.order = position
                activity.save(update_fields=["order"])


class Migration(migrations.Migration):

    dependencies = [
        ("processes", "0004_alter_activity_options_activity_order"),
    ]

    operations = [
        migrations.RunPython(backfill_activity_order, migrations.RunPython.noop),
    ]
