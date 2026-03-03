"""
Django settings for fuel_finder project.

Reads configuration from environment variables (or .env file).
Split into clear sections for easy environment overrides.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/5.1/ref/settings/
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env for local development; Docker Compose passes env vars directly.
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Core security
# ---------------------------------------------------------------------------
SECRET_KEY: str = os.environ.get(
    "SECRET_KEY",
    "django-insecure-change-me-in-production",
)

DEBUG: bool = os.environ.get("DEBUG", "True").lower() in ("1", "true", "yes")

ALLOWED_HOSTS: list[str] = os.environ.get(
    "ALLOWED_HOSTS", "localhost,127.0.0.1"
).split(",")

# ---------------------------------------------------------------------------
# Installed apps
# ---------------------------------------------------------------------------
INSTALLED_APPS: list[str] = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "corsheaders",
    # API documentation
    "drf_spectacular",
    # Local
    "stations.apps.StationsConfig",
    "router.apps.RouterConfig",
]

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
MIDDLEWARE: list[str] = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serves static files in prod
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF: str = "fuel_finder.urls"

# ---------------------------------------------------------------------------
# Templates  (project-level templates/ dir + app templates)
# ---------------------------------------------------------------------------
TEMPLATES: list[dict] = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION: str = "fuel_finder.wsgi.application"

# ---------------------------------------------------------------------------
# Database  — SQLite by default; switch to Postgres via DATABASE_URL env var
# ---------------------------------------------------------------------------
DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

if DATABASE_URL.startswith("postgresql"):
    # Parse  postgresql://user:pass@host:port/dbname
    _url = DATABASE_URL.replace("postgresql://", "")
    _credentials, _rest = _url.split("@")
    _user, _password = _credentials.split(":")
    _host_port, _dbname = _rest.split("/")
    _host, _port = (
        _host_port.split(":") if ":" in _host_port else (_host_port, "5432")
    )
    DATABASES: dict = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _dbname,
            "USER": _user,
            "PASSWORD": _password,
            "HOST": _host,
            "PORT": _port,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS: list[dict] = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE: str = "en-us"
TIME_ZONE: str = "UTC"
USE_I18N: bool = True
USE_TZ: bool = True

# ---------------------------------------------------------------------------
# Static files  (WhiteNoise compresses & serves them in both dev + prod)
# ---------------------------------------------------------------------------
STATIC_URL: str = "/static/"
STATICFILES_DIRS: list[Path] = [BASE_DIR / "static"]
STATIC_ROOT: str = str(BASE_DIR / "staticfiles")
STATICFILES_STORAGE: str = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# ---------------------------------------------------------------------------
# Default primary key
# ---------------------------------------------------------------------------
DEFAULT_AUTO_FIELD: str = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK: dict = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    # drf-spectacular auto-generates OpenAPI 3.0 schema from serializers
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# ---------------------------------------------------------------------------
# drf-spectacular — OpenAPI / Swagger configuration
# ---------------------------------------------------------------------------
SPECTACULAR_SETTINGS: dict = {
    "TITLE": "Fuel Route Planner API",
    "DESCRIPTION": (
        "Given an origin and destination within the USA, returns the optimal "
        "(cheapest) fuel stop plan along the route. Uses OSRM for routing and "
        "pre-geocoded OPIS station data for fuel prices."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,  # hide the raw schema endpoint from the UI list
    "CONTACT": {"name": "Fuel Route Planner"},
    "TAGS": [
        {"name": "Route", "description": "Route optimization endpoints"},
        {"name": "Health", "description": "Service health checks"},
    ],
}

# ---------------------------------------------------------------------------
# CORS  — allow all origins in dev; tighten in production
# ---------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS: bool = DEBUG

# ---------------------------------------------------------------------------
# External service configuration
# ---------------------------------------------------------------------------
# OSRM open-source routing engine.
# Public demo server works for small traffic; host your own for production.
# Docs: http://project-osrm.org/
OSRM_BASE_URL: str = os.environ.get(
    "OSRM_BASE_URL", "http://router.project-osrm.org"
)

# ---------------------------------------------------------------------------
# Vehicle assumptions (used by the fuel optimizer)
# ---------------------------------------------------------------------------
VEHICLE_MPG: float = 10.0              # miles per gallon
VEHICLE_TANK_RANGE_MILES: float = 500.0  # maximum range on a full tank
