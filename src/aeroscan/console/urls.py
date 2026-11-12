from __future__ import annotations

from django.contrib import admin
from django.urls import path

admin.site.site_header = "Aero-Scan admin"
admin.site.site_title = "Aero-Scan"
admin.site.index_title = "Alerts and reviews"

urlpatterns = [path("", admin.site.urls)]
