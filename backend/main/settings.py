"""This module configures django."""
import logging.config
import os
import pathlib
import shutil
import subprocess
import sys
from typing import List

import yaml
from django.core.management.utils import get_random_secret_key

from core.util import strtobool

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = strtobool(os.environ.get("DJANGO_DEMO", "0"))

try:
    with open(os.path.join(BASE_DIR, "config/secret_key.txt"), encoding="utf-8") as f:
        SECRET_KEY = f.read().strip()
except FileNotFoundError:
    SECRET_KEY = get_random_secret_key()
    with open(
        os.path.join(BASE_DIR, "config/secret_key.txt"), "w", encoding="utf-8"
    ) as f:
        f.write(SECRET_KEY)
    print("created secret key")

try:
    with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
        VERSION = f.read().strip()
except FileNotFoundError:
    VERSION = "undefined"

DEBUG = strtobool(os.environ.get("DJANGO_DEBUG", "0"))
TESTING = "test" in sys.argv

DOCKER = strtobool(os.environ.get("DOCKER", "0"))

ALLOWED_HOSTS = ["*"]

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

IPWARE_META_PRECEDENCE_ORDER = (
    "HTTP_X_FORWARDED_FOR",
    "HTTP_X_REAL_IP",
    "REMOTE_ADDR",
)
IPWARE_PROXY_COUNT = int(os.environ.get("IPWARE_PROXY_COUNT", "1"))

# Application definition

INSTALLED_APPS = [
    "core.apps.CoreConfig",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "django.contrib.postgres",
]

MIDDLEWARE = [
    "core.middleware.DatabaseConnectionCleanupMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.ClientIpBanMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.AfterHoursModeMiddleware",
]

ROOT_URLCONF = "main.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates/")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

CSRF_FAILURE_VIEW = "core.util.csrf_failure"

with open(os.path.join(BASE_DIR, "config/raveberry.yaml"), encoding="utf-8") as f:
    config = yaml.safe_load(f) or {}


def _config_or_env(config_key: str, env_key: str, default):
    value = config.get(config_key, default)
    if value in (None, ""):
        value = default

    env_value = os.environ.get(env_key, None)
    if env_value not in (None, ""):
        return env_value

    return value


remote_url = _config_or_env("remote_url", "REMOTE_URL", "https://furatic.xyz")
if remote_url and not remote_url.startswith(("https://", "http://")):
    remote_url = "https://" + remote_url
if remote_url:
    CSRF_TRUSTED_ORIGINS = [remote_url]

# Furatic is typically deployed behind a reverse proxy / tunnel.
# Trust the forwarded scheme and host so Django builds the correct origin during auth.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

WSGI_APPLICATION = "main.wsgi.application"

# Docker changes
if DOCKER:
    POSTGRES_HOST = "db"
    POSTGRES_PORT = "5432"
    REDIS_HOST = "redis"
    REDIS_PORT = "6379"
    MOPIDY_HOST = "mopidy"
    MOPIDY_PORT = "6680"
    ICECAST_HOST = "icecast"
    ICECAST_PORT = "8000"
    DEFAULT_CACHE_DIR = "/Music/raveberry/"
    TEST_CACHE_DIR = DEFAULT_CACHE_DIR
else:
    POSTGRES_HOST = "127.0.0.1"
    POSTGRES_PORT = "5432"
    REDIS_HOST = "127.0.0.1"
    REDIS_PORT = "6379"
    MOPIDY_HOST = "localhost"
    MOPIDY_PORT = "6680"
    ICECAST_HOST = "localhost"
    ICECAST_PORT = "8000"
    DEFAULT_CACHE_DIR = "~/Music/raveberry/"
    TEST_CACHE_DIR = os.path.join(BASE_DIR, "test_cache/")

# Allow these settings to be overwritten with environment variables
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "") or POSTGRES_HOST
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "") or POSTGRES_PORT
REDIS_HOST = os.environ.get("REDIS_HOST", "") or REDIS_HOST
REDIS_PORT = os.environ.get("REDIS_PORT", "") or REDIS_PORT
MOPIDY_HOST = os.environ.get("MOPIDY_HOST", "") or MOPIDY_HOST
MOPIDY_PORT = os.environ.get("MOPIDY_PORT", "") or MOPIDY_PORT
ICECAST_HOST = os.environ.get("ICECAST_HOST", "") or ICECAST_HOST
ICECAST_PORT = os.environ.get("ICECAST_PORT", "") or ICECAST_PORT

# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "raveberry"),
        "USER": os.environ.get("POSTGRES_USER", "raveberry"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "raveberry"),
        "HOST": POSTGRES_HOST,
        "PORT": POSTGRES_PORT,
    }
}

# Local Windows installs use password/trust authentication, not Kerberos.
# Avoid consulting the MIT credential-cache service on every connection.
# Keep Linux, remote databases, and explicit libpq preferences unchanged.
if os.name == "nt" and POSTGRES_HOST.lower() in {"127.0.0.1", "localhost", "::1"}:
    DATABASES["default"]["OPTIONS"] = {
        "gssencmode": os.environ.get("PGGSSENCMODE") or "disable",
    }

BROKER_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}"
CELERY_IMPORTS = [
    "core.lights.worker",
    "core.musiq.playback",
    "core.musiq.music_provider",
    "core.settings.library",
    "core.settings.sound",
]
CELERY_TASK_SERIALIZER = "pickle"
CELERY_ACCEPT_CONTENT = ["pickle"]
if TESTING:
    CELERY_ALWAYS_EAGER = True
    CELERY_EAGER_PROPAGATES_EXCEPTIONS = True

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# In order to avoid unexpected migrations when the default value is changed to BigAutoField,
# set it here explicitly.
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Berlin"
USE_I18N = True
USE_L10N = True
USE_TZ = True

# Users
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "logged-in"
LOGOUT_REDIRECT_URL = "base"
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 21
SESSION_SAVE_EVERY_REQUEST = True

trusted_proxy_ips = config.get("trusted_proxy_ips", ["127.0.0.1", "::1"])
env_trusted_proxy_ips = os.environ.get("TRUSTED_PROXY_IPS", "").strip()
if env_trusted_proxy_ips:
    trusted_proxy_ips = [
        ip.strip()
        for ip in env_trusted_proxy_ips.split(",")
        if ip.strip()
    ]
TRUSTED_PROXY_IPS = tuple(
    str(ip).strip() for ip in trusted_proxy_ips if str(ip).strip()
)

CLIENT_IP_HEADER_CANDIDATES = (
    "HTTP_X_FORWARDED_FOR",
    "HTTP_X_REAL_IP",
    "HTTP_CF_CONNECTING_IP",
    "HTTP_TRUE_CLIENT_IP",
    "HTTP_X_CLIENT_IP",
    "HTTP_FORWARDED",
)

FURATIC_PUBLIC_URL = remote_url or ""
FURATIC_DISCORD_INVITE_URL = _config_or_env(
    "furatic_discord_invite_url",
    "FURATIC_DISCORD_INVITE_URL",
    "",
)
FURATIC_VRCHAT_GROUP_URL = _config_or_env(
    "furatic_vrchat_group_url",
    "FURATIC_VRCHAT_GROUP_URL",
    "",
)
FURATIC_HLS_URL = _config_or_env(
    "furatic_hls_url",
    "FURATIC_HLS_URL",
    "https://aux.furatic.xyz:8888/live/index.m3u8",
)
FURATIC_LOGO_SQUARE_URL = _config_or_env(
    "furatic_logo_square_url",
    "FURATIC_LOGO_SQUARE_URL",
    "https://raw.githubusercontent.com/OrangeC7/raveberry/master/FuraticLogo.png",
)
FURATIC_LOGO_WIDE_URL = _config_or_env(
    "furatic_logo_wide_url",
    "FURATIC_LOGO_WIDE_URL",
    "https://raw.githubusercontent.com/OrangeC7/raveberry/master/FuraticLogoWide.png",
)
FURATIC_MOD_USERNAME = _config_or_env(
    "furatic_mod_username",
    "FURATIC_MOD_USERNAME",
    "mod",
)
FURATIC_MOD_PASSWORD = (
    os.environ.get("FURATIC_MOD_PASSWORD", "").strip()
    or os.environ.get("MOD_PASSWORD", "").strip()
    or str(config.get("furatic_mod_password", "") or "").strip()
)
FURATIC_OBS_OUTPUT_DIR = os.path.expanduser(
    _config_or_env(
        "furatic_obs_output_dir",
        "FURATIC_OBS_OUTPUT_DIR",
        "~/Documents/Raveberry",
    )
)

FURATIC_IP_BLOCKLIST_DIR = os.path.join(BASE_DIR, "config/ip_blocklists")
FURATIC_IP_SCREEN_CACHE_TTL_SECONDS = 86400
FURATIC_IP_INTEL_ENABLED = True
FURATIC_IP_INTEL_CONTACT_EMAIL = "contact@mglabs.dev"
FURATIC_IP_INTEL_FLAGS = "m"
FURATIC_IP_INTEL_BLOCK_THRESHOLD = 1
FURATIC_IP_INTEL_DAILY_LIMIT = 500
FURATIC_IP_INTEL_MINUTE_LIMIT = 15
FURATIC_IP_INTEL_TIMEOUT_SECONDS = 3

pathlib.Path(FURATIC_IP_BLOCKLIST_DIR).mkdir(parents=True, exist_ok=True)

FURATIC_IP_BLOCKLIST_DIR = os.path.expanduser(
    _config_or_env(
        "furatic_ip_blocklist_dir",
        "FURATIC_IP_BLOCKLIST_DIR",
        os.path.join(BASE_DIR, "config/ip_blocklists"),
    )
)

FURATIC_IP_SCREEN_CACHE_TTL_SECONDS = int(
    os.environ.get("FURATIC_IP_SCREEN_CACHE_TTL_SECONDS", "86400").strip() or "86400"
)

FURATIC_IP_INTEL_ENABLED = strtobool(
    str(
        _config_or_env(
            "furatic_ip_intel_enabled",
            "FURATIC_IP_INTEL_ENABLED",
            "1",
        )
    )
)

FURATIC_IP_INTEL_FLAGS = (
    os.environ.get("FURATIC_IP_INTEL_FLAGS", "").strip().lower()
    or str(config.get("furatic_ip_intel_flags", "") or "").strip().lower()
    or "m"
)
if FURATIC_IP_INTEL_FLAGS not in {"m", "b", "f"}:
    FURATIC_IP_INTEL_FLAGS = "m"

FURATIC_IP_INTEL_BLOCK_THRESHOLD = float(
    os.environ.get("FURATIC_IP_INTEL_BLOCK_THRESHOLD", "").strip()
    or str(config.get("furatic_ip_intel_block_threshold", "") or "").strip()
    or "0.99"
)

FURATIC_IP_INTEL_DAILY_LIMIT = int(
    os.environ.get("FURATIC_IP_INTEL_DAILY_LIMIT", "").strip()
    or str(config.get("furatic_ip_intel_daily_limit", "") or "").strip()
    or "500"
)

FURATIC_IP_INTEL_MINUTE_LIMIT = int(
    os.environ.get("FURATIC_IP_INTEL_MINUTE_LIMIT", "").strip()
    or str(config.get("furatic_ip_intel_minute_limit", "") or "").strip()
    or "15"
)

FURATIC_IP_INTEL_TIMEOUT_SECONDS = float(
    os.environ.get("FURATIC_IP_INTEL_TIMEOUT_SECONDS", "").strip()
    or str(config.get("furatic_ip_intel_timeout_seconds", "") or "").strip()
    or "2.5"
)

pathlib.Path(FURATIC_IP_BLOCKLIST_DIR).mkdir(parents=True, exist_ok=True)

# Static files (CSS, JavaScript, Images)
STATIC_FILES = os.path.join(BASE_DIR, "static")
STATICFILES_DIRS: List[str] = [STATIC_FILES]
STATIC_URL = "/static/"

if not os.path.exists(os.path.join(BASE_DIR, "static/admin")):
    import django

    DJANGO_PATH = os.path.dirname(django.__file__)
    STATIC_ADMIN = os.path.join(DJANGO_PATH, "contrib/admin/static/admin")
    STATIC_ADMIN_DEST = os.path.join(BASE_DIR, "static/admin")
    if not DOCKER:
        # create symlink to admin static files if not present
        # In the docker setup, the nginx container includes the static files on build
        try:
            os.symlink(
                STATIC_ADMIN,
                STATIC_ADMIN_DEST,
                target_is_directory=True,
            )
            print("linked static admin files")
        except FileExistsError:
            # Sometimes this happened during installation, even though we check right before,
            # maybe because of a race condition.
            # The try/except should prevent crashing the installation.
            pass
        except OSError as exc:
            # Windows often blocks symlink creation without Developer Mode
            # or the right privilege. Fall back to copying the admin files.
            if os.name == "nt" or getattr(exc, "winerror", None) == 1314:
                shutil.copytree(STATIC_ADMIN, STATIC_ADMIN_DEST, dirs_exist_ok=True)
                print("copied static admin files")
            else:
                raise

# channels
ASGI_APPLICATION = "main.routing.APPLICATION"

if "runserver" in sys.argv or strtobool(os.environ.get("FURATIC_LOCAL_HTTP_SERVER", "0")):
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [(REDIS_HOST, REDIS_PORT)],
                "capacity": 1500,
                "expiry": 10,
            },
        }
    }

# Logging

# avoid creating a logger in every module
logging.basicConfig()

# Logging failures due to Windows Defender scanning our files must never take down the site. 
# This keeps logging internals from surfacing transient Windows file lock errors due to OS software.
logging.raiseExceptions = DEBUG or TESTING

FURATIC_LOG_MAX_BYTES = int(
    os.environ.get("FURATIC_LOG_MAX_BYTES", str(1024 * 1024 * 15))
)
FURATIC_LOG_BACKUP_COUNT = int(
    os.environ.get("FURATIC_LOG_BACKUP_COUNT", "10")
)
FURATIC_LOG_RETRY_ATTEMPTS = int(
    os.environ.get("FURATIC_LOG_RETRY_ATTEMPTS", "8")
)
FURATIC_LOG_RETRY_INITIAL_DELAY_SECONDS = float(
    os.environ.get("FURATIC_LOG_RETRY_INITIAL_DELAY_SECONDS", "0.25")
)
FURATIC_LOG_RETRY_MAX_DELAY_SECONDS = float(
    os.environ.get("FURATIC_LOG_RETRY_MAX_DELAY_SECONDS", "2.0")
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "precise": {
            "format": "%(asctime)s %(module)s.%(funcName)s:%(lineno)s %(levelname)s  %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "brief": {
            "format": "%(module)s.%(funcName)s:%(lineno)s %(levelname)s  %(message)s"
        },
    },
    "handlers": {
        "infofile": {
            "level": "INFO",
            "formatter": "precise",
            "class": "core.logging_handlers.WindowsSafeRotatingFileHandler",
            "filename": os.path.join(BASE_DIR, "logs/info.log"),
            "maxBytes": FURATIC_LOG_MAX_BYTES,
            "backupCount": FURATIC_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "retry_attempts": FURATIC_LOG_RETRY_ATTEMPTS,
            "retry_initial_delay_seconds": FURATIC_LOG_RETRY_INITIAL_DELAY_SECONDS,
            "retry_max_delay_seconds": FURATIC_LOG_RETRY_MAX_DELAY_SECONDS,
        },
        "errorfile": {
            "level": "ERROR",
            "formatter": "precise",
            "class": "core.logging_handlers.WindowsSafeRotatingFileHandler",
            "filename": os.path.join(BASE_DIR, "logs/error.log"),
            "maxBytes": FURATIC_LOG_MAX_BYTES,
            "backupCount": FURATIC_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "retry_attempts": FURATIC_LOG_RETRY_ATTEMPTS,
            "retry_initial_delay_seconds": FURATIC_LOG_RETRY_INITIAL_DELAY_SECONDS,
            "retry_max_delay_seconds": FURATIC_LOG_RETRY_MAX_DELAY_SECONDS,
        },
        "console": {
            "level": "INFO",
            "formatter": "brief",
            "class": "logging.StreamHandler",
        },
        "docker": {
            "level": "INFO",
            "formatter": "precise",
            "class": "logging.StreamHandler",
        },
    },
    "loggers": {
        "django.server": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "daphne": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "daphne.server": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "daphne.http_protocol": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "daphne.ws_protocol": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.channels": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "django.channels.server": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["infofile", "errorfile", "console"]
        if DEBUG or TESTING
        else ["infofile", "errorfile", "docker"]
        if DOCKER
        else ["infofile", "errorfile"],
        "level": "DEBUG",
    },
}
LOGGING_CONFIG = None  # disables Django handling of logging
logging.config.dictConfig(LOGGING)

# In local networks Raveberry is only accessible via http, secure cookies are not possible there.
# If Raveberry is only used with https, these should be enabled.
# SESSION_COOKIE_SECURE = True
# CSRF_COOKIE_SECURE = True

# app settings
try:
    with open(os.path.join(BASE_DIR, "config/cache_dir"), encoding="utf-8") as f:
        SONGS_CACHE_DIR = f.read().strip()
except FileNotFoundError:
    SONGS_CACHE_DIR = ""
if SONGS_CACHE_DIR == "":
    SONGS_CACHE_DIR = os.path.expanduser(DEFAULT_CACHE_DIR)
    with open(os.path.join(BASE_DIR, "config/cache_dir"), "w", encoding="utf-8") as f:
        f.write(SONGS_CACHE_DIR)
    print(f"no song caching directory specified, using {DEFAULT_CACHE_DIR}")

# sound
PULSE_SERVER = "127.0.0.1"
if DEBUG:
    try:
        output = subprocess.check_output(
            "pactl info".split(), text=True, stderr=subprocess.DEVNULL
        )
        PULSE_SERVER = output.splitlines()[0].split(":")[1].strip()
    except (FileNotFoundError, subprocess.CalledProcessError, IndexError):
        pass

# use a different cache directory for testing
if TESTING:
    SONGS_CACHE_DIR = TEST_CACHE_DIR

pathlib.Path(SONGS_CACHE_DIR).mkdir(parents=True, exist_ok=True)

