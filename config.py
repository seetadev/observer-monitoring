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

# Address the HTTP and TCP servers bind to. Loopback by default; the Docker image sets 0.0.0.0.
BIND_ADDRESS = os.environ.get("OBSERVER_BIND", "127.0.0.1")

# Tornado autoreload and debug helpers. Off by default.
DEBUG = _flag("OBSERVER_DEBUG")

STATIC_DIR = os.path.join(BASE_DIR, "static")
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
FILES_DIR = os.environ.get("OBSERVER_FILES_DIR", os.path.join(BASE_DIR, "files_buffer"))
