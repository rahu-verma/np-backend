# Generated by Django 5.0.6 on 2024-09-02 15:39

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('inventory', '0029_brandsupplier_brand_suppliers_supplier_brands'),
    ]

    operations = [
        migrations.AlterField(
            model_name='product',
            name='product_quantity',
            field=models.IntegerField(default=2147483647),
        ),
    ]
