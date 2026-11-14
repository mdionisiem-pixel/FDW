"""Repair goals saved through the old 'goal (measure; by when)' text boxes.

A date with its own brackets, such as 'December (year not stated by founder)',
left the whole line in the goal; a measure containing ';' was split in the
wrong place. Goals are now edited as separate fields, and this fixes what
was already saved, in the brief and its earlier versions.
"""
from django.db import migrations


def repair(apps, schema_editor):
    from workflow.analyst import repair_goal

    for name in ("BrandBrief", "BrandBriefVersion"):
        model = apps.get_model("workflow", name)
        for row in model.objects.all():
            fixed = [repair_goal(g) for g in row.goals or []]
            if fixed != (row.goals or []):
                row.goals = fixed
                row.save(update_fields=["goals"])


class Migration(migrations.Migration):
    dependencies = [("workflow", "0014_brand_analyst")]
    operations = [migrations.RunPython(repair, migrations.RunPython.noop)]
