"""Authentication for the dashboard, its services and the agent channel.

* Admin: one account whose password is stored only as an scrypt hash (environment). A successful login sets a
  signed, expiring, HttpOnly session cookie. State-changing requests also need the XSRF token.
* Agents: each agent has a random token; only its SHA-256 digest is stored. The first line an agent sends on the
  TCP channel must carry the token.
"""
import asyncio
import base64
import datetime
import hashlib
import hmac
import os
import re
import secrets
import time
from urllib.parse import urlsplit

import tornado.web

import config
from db import get_db

SESSION_COOKIE = "observer_session"
MIN_PASSWORD_LENGTH = 12
MIN_SECRET_LENGTH = 32
NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_SCRYPT_MAX_N = 2 ** 16
_SCRYPT_MAXMEM = 128 * 1024 * 1024


class ConfigError(RuntimeError):
    """Invalid or missing security configuration."""


# --- password hashing ------------------------------------------------------------------------------------------

def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=_SCRYPT_MAXMEM)


def password_is_acceptable(password):
    return isinstance(password, str) and len(password) >= MIN_PASSWORD_LENGTH


def hash_password(password):
    """Return 'scrypt:N:r:p:salt:hash'. Refuses passwords shorter than MIN_PASSWORD_LENGTH."""
    if not password_is_acceptable(password):
        raise ValueError("password must be at least %d characters" % MIN_PASSWORD_LENGTH)
    salt = os.urandom(16)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return "scrypt:%d:%d:%d:%s:%s" % (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _b64(salt), _b64(digest))


def parse_hash(stored):
    """Return (n, r, p, salt, digest) or raise ValueError."""
    parts = stored.split(":")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("not an scrypt hash")
    n, r, p = (int(x) for x in parts[1:4])
    if not (2 <= n <= _SCRYPT_MAX_N and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4):
        raise ValueError("scrypt parameters out of range")
    return n, r, p, _unb64(parts[4]), _unb64(parts[5])


def verify_password(password, stored):
    """Constant-time check. False for any malformed hash or unacceptable password."""
    if not password_is_acceptable(password) or not stored:
        return False
    try:
        n, r, p, salt, expected = parse_hash(stored)
        actual = _scrypt(password, salt, n, r, p)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# --- start-up validation ---------------------------------------------------------------------------------------

def is_production():
    return config.ENV == "production"


def cookie_secure():
    return is_production() if config.COOKIE_SECURE is None else config.COOKIE_SECURE


def validate_config():
    """Raise ConfigError if the security configuration is unusable. Called before any server starts."""
    problems = []
    if config.ENV not in ("production", "development"):
        problems.append("OBSERVER_ENV must be 'production' or 'development'")
    secret = config.COOKIE_SECRET
    if len(secret) < MIN_SECRET_LENGTH or len(set(secret)) < 8:
        problems.append("OBSERVER_COOKIE_SECRET must be set to a random value of at least %d characters "
                        "(openssl rand -hex 32)" % MIN_SECRET_LENGTH)
    stored = config.ADMIN_PASSWORD_HASH
    if stored:
        try:
            parse_hash(stored)
        except (ValueError, TypeError):
            problems.append("OBSERVER_ADMIN_PASSWORD_HASH is not a valid hash (generate one with: "
                            "python run.py hash-password)")
    elif is_production():
        problems.append("OBSERVER_ADMIN_PASSWORD_HASH must be set in production "
                        "(generate one with: python run.py hash-password)")
    if problems:
        raise ConfigError("; ".join(problems))


# --- sessions, CORS, handlers ----------------------------------------------------------------------------------

def origin_allowed(origin, host):
    """Exact allow-list: configured origins, or the request's own host on the dashboard port."""
    origin = origin.rstrip("/")
    if origin in config.ALLOWED_ORIGINS:
        return True
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    request_host = host.rsplit(":", 1)[0].strip("[]") if ":" in host else host
    return parts.hostname == request_host and port == config.DASH_PORT


def safe_next(value):
    """Only same-site absolute paths are accepted as a post-login target."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value and "\n" not in value:
        return value
    return "/live"


class SessionMixin:
    def get_current_user(self):
        value = self.get_signed_cookie(SESSION_COOKIE, max_age_days=config.SESSION_TTL_SECONDS / 86400.0)
        return "admin" if value == b"admin" else None

    def start_session(self):
        self.set_signed_cookie(SESSION_COOKIE, "admin", expires_days=config.SESSION_TTL_SECONDS / 86400.0,
                               httponly=True, samesite="Lax", secure=cookie_secure())

    def end_session(self):
        self.clear_cookie(SESSION_COOKIE)


class BaseHandler(SessionMixin, tornado.web.RequestHandler):
    def set_default_headers(self):
        origin = self.request.headers.get("Origin")
        if origin and origin_allowed(origin, self.request.host):
            self.set_header("Access-Control-Allow-Origin", origin)
            self.set_header("Access-Control-Allow-Credentials", "true")
        self.set_header("Vary", "Origin")

    def options(self, *args, **kwargs):
        self.set_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "X-XSRFToken, Content-Type")
        self.set_header("Access-Control-Max-Age", "600")
        self.set_status(204)
        self.finish()

    def error_respond(self, code, msg):
        self.set_status(code)
        self.write({'status': code, 'message': msg})
        self.finish()


class ApiBase(BaseHandler):
    """JSON endpoints: authenticated unless requires_auth() says otherwise."""

    def requires_auth(self):
        return True

    def prepare(self):
        if self.request.method != "OPTIONS" and self.requires_auth() and not self.current_user:
            self.error_respond(401, "Authentication required")


class PageHandler(BaseHandler):
    """HTML pages: redirect to the login page when there is no session; always issue the XSRF cookie."""

    def prepare(self):
        self.xsrf_token  # noqa: B018  (touching it sets the cookie)


class AuthenticatedStaticFileHandler(SessionMixin, tornado.web.StaticFileHandler):
    def prepare(self):
        if not self.current_user:
            self.set_status(401)
            self.finish()


class LoginLimiter:
    """Per-address failed-login limit (in memory, per process)."""

    def __init__(self, attempts=5, window=300):
        self.attempts, self.window, self.failures = attempts, window, {}

    def _recent(self, key):
        now = time.monotonic()
        recent = [t for t in self.failures.get(key, []) if now - t < self.window]
        self.failures[key] = recent
        return recent

    def blocked(self, key):
        return len(self._recent(key)) >= self.attempts

    def record_failure(self, key):
        self._recent(key).append(time.monotonic())

    def reset(self, key):
        self.failures.pop(key, None)


login_limiter = LoginLimiter()


def app_settings(**extra):
    return dict(template_path=config.BASE_DIR, autoreload=config.DEBUG, cookie_secret=config.COOKIE_SECRET,
                xsrf_cookies=True, xsrf_cookie_kwargs={"samesite": "Lax", "secure": cookie_secure()},
                login_url="/login", **extra)


# --- agent tokens ----------------------------------------------------------------------------------------------

def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def ensure_indexes():
    agents = get_db()["agents"]
    await agents.create_index("token_sha256", unique=True)
    await agents.create_index("name", unique=True)


async def create_agent(name):
    """Register an agent and return its token. The token is not stored, only its digest."""
    if not NAME_PATTERN.match(name):
        raise ValueError("agent name must match [A-Za-z0-9._-]{1,64}")
    await ensure_indexes()
    token = secrets.token_urlsafe(32)
    try:
        await get_db()["agents"].insert_one({"name": name, "token_sha256": token_digest(token),
                                             "created": datetime.datetime.now(datetime.timezone.utc)})
    except Exception as exc:
        if exc.__class__.__name__ == "DuplicateKeyError":
            raise ValueError("an agent named %r already exists" % name) from None
        raise
    return token


async def find_agent(token):
    if not isinstance(token, str) or not token:
        return None
    return await get_db()["agents"].find_one({"token_sha256": token_digest(token)})


async def agent_exists(name):
    return await get_db()["agents"].count_documents({"name": name}, limit=1) == 1


async def list_agents():
    return [doc["name"] async for doc in get_db()["agents"].find().sort("name", 1)]


async def revoke_agent(name):
    result = await get_db()["agents"].delete_one({"name": name})
    return result.deleted_count == 1


def run(coro):
    return asyncio.run(coro)
