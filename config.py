"""Runtime configuration, read from environment variables.

Defaults are suitable for running everything on one machine.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# MongoDB connection (the original code hardcoded 127.0.0.1:27017 and database "observerdb").
MONGO_URL = os.environ.get("OBSERVER_MONGO_URL", "mongodb://127.0.0.1:27017")
DB_NAME = os.environ.get("OBSERVER_DB_NAME", "observerdb")

# Retention. Live samples are 1-second raw data that are condensed into 1-minute statistics; a TTL removes a
# buffer that was left behind by an agent that stopped. Statistics are kept forever unless a retention is set.
LIVE_TTL_SECONDS = int(os.environ.get("OBSERVER_LIVE_TTL_SECONDS", "3600"))
STAT_RETENTION_DAYS = int(os.environ.get("OBSERVER_STAT_RETENTION_DAYS", "0"))

# Address the HTTP and TCP servers bind to. Loopback by default; the Docker image sets 0.0.0.0.
BIND_ADDRESS = os.environ.get("OBSERVER_BIND", "127.0.0.1")

# Tornado autoreload and debug helpers. Off by default.
DEBUG = _flag("OBSERVER_DEBUG")

STATIC_DIR = os.path.join(BASE_DIR, "static")
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
FILES_DIR = os.environ.get("OBSERVER_FILES_DIR", os.path.join(BASE_DIR, "files_buffer"))


# --- Access control -------------------------------------------------------------------------------------------
# "production" refuses to start without a valid admin credential and cookie secret, and uses Secure cookies.
ENV = os.environ.get("OBSERVER_ENV", "development").strip().lower()

# scrypt hash of the admin password. Generate with: python run.py hash-password
ADMIN_PASSWORD_HASH = os.environ.get("OBSERVER_ADMIN_PASSWORD_HASH", "").strip()

# Signs session cookies. Must be identical for every Observer process (dashboard, live, compute, simulator).
COOKIE_SECRET = os.environ.get("OBSERVER_COOKIE_SECRET", "")

# None = Secure cookies in production only; "1"/"0" forces the setting.
_secure = os.environ.get("OBSERVER_COOKIE_SECURE")
COOKIE_SECURE = None if _secure in (None, "") else _flag("OBSERVER_COOKIE_SECURE")

SESSION_TTL_SECONDS = int(os.environ.get("OBSERVER_SESSION_TTL_SECONDS", str(8 * 3600)))

# The dashboard origin(s) that may call the live/compute/simulator services from the browser.
# Default: same host as the request, on the dashboard port.
DASH_PORT = int(os.environ.get("OBSERVER_DASH_PORT", "9000"))
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("OBSERVER_ALLOWED_ORIGINS", "").split(",")
                   if o.strip()]
