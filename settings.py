import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-only-change-me-before-deploying-release-workbench",
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "workbench",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "workbench.urls"
MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 20},
    }
}

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

WORKBENCH_OPERATOR = os.environ.get("WORKBENCH_OPERATOR", "本机操作员")
WORKBENCH_GIT_TIMEOUT_SECONDS = int(
    os.environ.get("WORKBENCH_GIT_TIMEOUT_SECONDS", "120")
)
WORKBENCH_BUILD_TIMEOUT_SECONDS = int(
    os.environ.get("WORKBENCH_BUILD_TIMEOUT_SECONDS", "1800")
)
WORKBENCH_BUILD_LOG_LIMIT = int(os.environ.get("WORKBENCH_BUILD_LOG_LIMIT", "4000"))
