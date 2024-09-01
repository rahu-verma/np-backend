# Generated by Django 5.0.3 on 2024-06-06 14:16

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('inventory', '0015_alter_supplier_phone_number'),
    ]

    operations = [
        migrations.AlterField(
            model_name='supplier',
            name='email',
            field=models.CharField(
                blank=True,
                max_length=255,
                null=True,
                validators=[django.core.validators.EmailValidator()],
            ),
        ),
    ]