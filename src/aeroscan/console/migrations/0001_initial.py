from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Alert",
            fields=[
                ("id", models.IntegerField(primary_key=True, serialize=False)),
                ("detected_at", models.DateTimeField()),
                ("rule", models.TextField()),
                ("severity", models.TextField()),
                ("icao24", models.TextField(verbose_name="ICAO24")),
                ("callsign", models.TextField(null=True)),
                ("summary", models.TextField()),
                ("evidence", models.JSONField()),
                ("p_real", models.FloatField(null=True, verbose_name="triage p(real)")),
            ],
            options={
                "db_table": "alerts",
                "ordering": ("-detected_at",),
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="Poll",
            fields=[
                ("polled_at", models.DateTimeField(primary_key=True, serialize=False)),
                ("aircraft", models.IntegerField()),
                ("alerts", models.IntegerField()),
                ("suppressed", models.IntegerField()),
                ("credits_remaining", models.IntegerField(null=True)),
            ],
            options={
                "db_table": "polls",
                "ordering": ("-polled_at",),
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="Position",
            fields=[
                (
                    "icao24",
                    models.TextField(primary_key=True, serialize=False, verbose_name="ICAO24"),
                ),
                ("callsign", models.TextField(null=True)),
                ("seen_at", models.DateTimeField()),
                ("latitude", models.FloatField()),
                ("longitude", models.FloatField()),
                ("altitude_ft", models.FloatField(null=True)),
                ("groundspeed_kt", models.FloatField(null=True)),
                ("track_deg", models.FloatField(null=True)),
                ("vertical_rate_fpm", models.FloatField(null=True)),
                ("squawk", models.TextField(null=True)),
                ("on_ground", models.BooleanField()),
            ],
            options={
                "verbose_name": "aircraft (latest poll)",
                "verbose_name_plural": "aircraft (latest poll)",
                "db_table": "positions",
                "ordering": ("callsign",),
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="Review",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("alert_id", models.IntegerField(unique=True)),
                (
                    "verdict",
                    models.CharField(
                        choices=[
                            ("real", "Real event"),
                            ("false_alarm", "False alarm"),
                            ("unsure", "Unsure"),
                        ],
                        max_length=16,
                    ),
                ),
                ("note", models.TextField(blank=True)),
                ("reviewed_by", models.CharField(blank=True, max_length=150)),
                ("reviewed_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ("-reviewed_at",),
            },
        ),
    ]
