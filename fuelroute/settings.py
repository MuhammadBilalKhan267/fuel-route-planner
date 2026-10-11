import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)
DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "local-development-only-fuelroute")
if not DEBUG and SECRET_KEY == "local-development-only-fuelroute":
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY when DJANGO_DEBUG=false.")
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.staticfiles", "trips"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "fuelroute.urls"
WSGI_APPLICATION = "fuelroute.wsgi.application"
ASGI_APPLICATION = "fuelroute.asgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
DATA_UPLOAD_MAX_MEMORY_SIZE = 16_384

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": str(BASE_DIR / ".cache" / "responses"),
        "OPTIONS": {"MAX_ENTRIES": 5000},
    }
}

ORS_API_KEY = os.getenv("ORS_API_KEY", "")
ORS_API_BASE_URL = "https://api.heigit.org"
PROVIDER_TIMEOUT_SECONDS = 12
GEOCODE_CACHE_SECONDS = 30 * 24 * 3600
ROUTE_CACHE_SECONDS = 24 * 3600
DEFAULT_INITIAL_FUEL_GALLONS = os.getenv("DEFAULT_INITIAL_FUEL_GALLONS", "50")
DEFAULT_INITIAL_FUEL_PRICE_USD = os.getenv("DEFAULT_INITIAL_FUEL_PRICE_USD", "0")
DEFAULT_VERIFY_STOPS = os.getenv("DEFAULT_VERIFY_STOPS", "true").lower() == "true"
DEFAULT_STATION_CORRIDOR_MILES = os.getenv("DEFAULT_STATION_CORRIDOR_MILES", "2")
MAP_TILE_URL = os.getenv(
    "MAP_TILE_URL",
    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
)
MAP_TILE_ATTRIBUTION = os.getenv(
    "MAP_TILE_ATTRIBUTION",
    '<a href="https://www.usgs.gov/programs/national-geospatial-program/national-map">USGS The National Map</a>',
)
