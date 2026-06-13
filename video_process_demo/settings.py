import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-demo-video-process-analysis-local-only",
)

DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() in {"1", "true", "yes", "on"}

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "processes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "video_process_demo.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "video_process_demo.wsgi.application"

database_url = os.getenv("DATABASE_URL", "")

if database_url.startswith(("postgres://", "postgresql://")):
    parsed = urlparse(database_url)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"),
            "USER": parsed.username or "",
            "PASSWORD": parsed.password or "",
            "HOST": parsed.hostname or "",
            "PORT": parsed.port or "",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "pl"
LANGUAGES = [
    ("pl", "Polski"),
    ("en", "English"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = os.getenv("TIME_ZONE", "Europe/Warsaw")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

DATA_UPLOAD_MAX_MEMORY_SIZE = int(
    os.getenv("DATA_UPLOAD_MAX_MEMORY_SIZE", 1024 * 1024 * 1024)
)
FILE_UPLOAD_MAX_MEMORY_SIZE = int(
    os.getenv("FILE_UPLOAD_MAX_MEMORY_SIZE", 10 * 1024 * 1024)
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.5-flash")
GEMINI_VIDEO_MODEL = os.getenv("GEMINI_VIDEO_MODEL", "gemini-3.5-flash")


def _model_choices_env(name, default):
    choices = []
    for raw_item in os.getenv(name, default).split(","):
        item = raw_item.strip()
        if not item:
            continue
        value, _, label = item.partition(":")
        value = value.strip()
        label = label.strip() or value
        if value and value not in {choice[0] for choice in choices}:
            choices.append((value, label))
    if GEMINI_VIDEO_MODEL and GEMINI_VIDEO_MODEL not in {choice[0] for choice in choices}:
        choices.insert(0, (GEMINI_VIDEO_MODEL, f"{GEMINI_VIDEO_MODEL} (domyślny)"))
    return choices


GEMINI_VIDEO_MODEL_CHOICES = _model_choices_env(
    "GEMINI_VIDEO_MODEL_CHOICES",
    ",".join(
        [
            "gemini-3.5-flash:Gemini 3.5 Flash - aktualny/stabilny",
            "gemini-3.1-pro-preview:Gemini 3.1 Pro preview - dokładniejsze rozumowanie",
            "gemini-3-flash-preview:Gemini 3 Flash preview - starszy preview",
            "gemini-3.1-flash-lite:Gemini 3.1 Flash-Lite - szybciej/taniej",
            "gemini-flash-latest:Gemini Flash latest",
            "gemini-pro-latest:Gemini Pro latest",
        ]
    ),
)
GEMINI_USE_MOCK = os.getenv("GEMINI_USE_MOCK", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GEMINI_FALLBACK_TO_MOCK = os.getenv("GEMINI_FALLBACK_TO_MOCK", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _float_env(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


# Szacowanie kosztu analizy. Ceny są przybliżone i konfigurowalne — sprawdź
# aktualny cennik wybranego modelu Gemini i ustaw poniższe zmienne w .env.
# Wartości w USD za 1 mln tokenów.
GEMINI_PRICE_INPUT_PER_M = _float_env("GEMINI_PRICE_INPUT_PER_M", 0.30)
GEMINI_PRICE_OUTPUT_PER_M = _float_env("GEMINI_PRICE_OUTPUT_PER_M", 2.50)
# Przybliżona liczba tokenów na sekundę wideo (do szacunku w trybie mock,
# gdy API nie zwraca realnego zużycia tokenów).
GEMINI_VIDEO_TOKENS_PER_SECOND = _float_env("GEMINI_VIDEO_TOKENS_PER_SECOND", 263.0)
# Kurs USD->PLN tylko do orientacyjnego przeliczenia w UI.
GEMINI_USD_PLN_RATE = _float_env("GEMINI_USD_PLN_RATE", 4.00)

# OpenAI — używane WYŁĄCZNIE do opisów czynności (analiza wideo zostaje na Gemini).
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")
OPENAI_USE_MOCK = os.getenv("OPENAI_USE_MOCK", "false").lower() in {
    "1",
    "true",
    "yes",
}
