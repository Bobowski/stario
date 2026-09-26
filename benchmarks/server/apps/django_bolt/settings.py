"""Minimal Django project for django-bolt benchmarks."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "benchmark-only-not-for-production"
DEBUG = False
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django_bolt.apps.DjangoBoltConfig",
]

MIDDLEWARE = []

ROOT_URLCONF = "apps.django_bolt.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

USE_TZ = True

BOLT_API = ["apps.django_bolt.api:api"]
BOLT_MAX_UPLOAD_SIZE = 3 * 1024 * 1024
BOLT_COMPRESSION = None
