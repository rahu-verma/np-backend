# Generated by Django 5.0.3 on 2024-06-06 13:17

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('campaign', '0012_alter_employee_phone_number_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='organization',
            name='custom_text_color',
        ),
        migrations.RemoveField(
            model_name='organization',
            name='custom_text_font',
        ),
    ]