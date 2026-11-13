"""Start the supplier table with the three suppliers in the specification.

Every fact is what each supplier's own page stated when it was read on
12 November 2026. Fields the page did not state are left empty, so the
sourcing check treats them as unknown. The founder edits and adds to this
table on the Suppliers screen.
"""
import datetime

from django.db import migrations

CHECKED = datetime.date(2026, 11, 12)

SUPPLIERS = [
    {
        "name": "Printful knitwear",
        "route": "on_demand",
        "makes": "Fully knitted products (product list not stated on the page)",
        "techniques": "Machine knitting with up to 4 yarn colours per product",
        "limits": "4 yarn colours per product, chosen from 36",
        "minimum_order": "No minimum order",
        "source_url": "https://www.printful.com/knitting",
    },
    {
        "name": "Knitwise",
        "route": "on_demand",
        "makes": "Sweaters, cardigans, vests, beanies, blankets",
        "techniques": "4-colour jacquard knit",
        "limits": "4 colours per design",
        "minimum_order": "1",
        "lead_time": "7 to 14 days production",
        "prices": "Typical sweaters $27 to $49",
        "price_currency": "USD",
        "notes": "Offers a Shopify app.",
        "source_url": "https://www.knitwise.com/collections/knit-on-demand-for-business",
    },
    {
        "name": "Wildemasche",
        "route": "on_demand",
        "makes": "Jacquard-knitted products (product list not stated on the page)",
        "techniques": "Jacquard knit",
        "minimum_order": "No minimum for prototypes",
        "lead_time": "About 1 to 2 weeks for single pieces",
        "location": "Germany",
        "source_url": "https://www.wildemasche.com/en/Knit-on-demand/",
    },
]


def seed(apps, schema_editor):
    Supplier = apps.get_model("workflow", "Supplier")
    for data in SUPPLIERS:
        Supplier.objects.get_or_create(name=data["name"], defaults={**data, "checked_on": CHECKED})


def unseed(apps, schema_editor):
    Supplier = apps.get_model("workflow", "Supplier")
    Supplier.objects.filter(name__in=[s["name"] for s in SUPPLIERS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("workflow", "0007_sourcing_check"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
