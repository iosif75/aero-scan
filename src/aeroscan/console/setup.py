"""Configure Django for one monitor database, and build the ASGI app to mount."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

SECRET_ENV = "AEROSCAN_SECRET_KEY"
URL_PREFIX = "/admin"

_configured: tuple[Path, Path] | None = None


def admin_db_for(db_path: Path | str) -> Path:
    """Default admin database: next to the monitor's, e.g. alerts.sqlite -> alerts.admin.sqlite."""
    p = Path(db_path)
    return p.with_name(f"{p.stem}.admin{p.suffix or '.sqlite'}")


def _secret_key(admin_db: Path) -> str:
    """From the environment, else a key generated once and kept beside the admin database."""
    if key := os.environ.get(SECRET_ENV):
        return key
    path = admin_db.with_name(admin_db.name + ".secret")
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(50)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


def configure(db_path: Path | str, admin_db: Path | str | None = None) -> None:
    """Point Django at a monitor database (read-only) and an admin database, and migrate
    the admin one. Django settings are per process: a second call must name the same files."""
    global _configured
    import django
    from django.conf import settings
    from django.core.management import call_command

    monitor = Path(db_path).resolve()
    admin = Path(admin_db).resolve() if admin_db is not None else admin_db_for(monitor)
    if _configured is not None:
        if _configured != (monitor, admin):
            raise RuntimeError(f"Django is already configured for {_configured[0]}")
        return
    databases: dict[str, Any] = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(admin)},
        # Django opens SQLite with uri=True, so this is the same read-only open as the API's.
        "monitor": {"ENGINE": "django.db.backends.sqlite3", "NAME": f"file:{monitor}?mode=ro"},
    }
    settings.configure(
        SECRET_KEY=_secret_key(admin),
        DEBUG=False,
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF="aeroscan.console.urls",
        FORCE_SCRIPT_NAME=None,
        USE_TZ=True,
        TIME_ZONE="UTC",
        INSTALLED_APPS=[
            "django.contrib.admin",
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "django.contrib.messages",
            "django.contrib.staticfiles",
            "aeroscan.console",
        ],
        MIDDLEWARE=[
            "django.middleware.security.SecurityMiddleware",
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
            "django.middleware.clickjacking.XFrameOptionsMiddleware",
        ],
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "APP_DIRS": True,
                "OPTIONS": {
                    "context_processors": [
                        "django.template.context_processors.request",
                        "django.contrib.auth.context_processors.auth",
                        "django.contrib.messages.context_processors.messages",
                    ]
                },
            }
        ],
        DATABASES=databases,
        DATABASE_ROUTERS=["aeroscan.console.models.MonitorRouter"],
        STATIC_URL=f"{URL_PREFIX}/static/",
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        LOGGING_CONFIG=None,
    )
    django.setup()
    admin.parent.mkdir(parents=True, exist_ok=True)
    call_command("migrate", database="default", interactive=False, verbosity=0)
    _configured = (monitor, admin)


def static_dir() -> Path:
    """The admin's CSS and JS, served by the host app (Django serves no static files itself)."""
    import django.contrib.admin

    return Path(django.contrib.admin.__file__).parent / "static"


def asgi_app() -> Any:
    from django.core.asgi import get_asgi_application

    return get_asgi_application()
