"""Shared fixtures.

The tests need a MongoDB (TEST_MONGO_URL, default mongodb://127.0.0.1:27017). Every test gets its own
throw-away database whose name starts with "observer_test_"; it is dropped afterwards, and the fixture
refuses to drop anything else.
"""
import os
import sys
import uuid

import httpx
import pytest
import pytest_asyncio
from tornado.httpserver import HTTPServer
from tornado.testing import bind_unused_port
from tornado.web import create_signed_value

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402
import config  # noqa: E402
import db as db_module  # noqa: E402

TEST_MONGO_URL = os.environ.get("TEST_MONGO_URL", "mongodb://127.0.0.1:27017")
TEST_PASSWORD = "test-only passphrase 0000"
TEST_SECRET = "0123456789abcdef" * 4
_TEST_HASH = auth.hash_password(TEST_PASSWORD)


@pytest.fixture(autouse=True)
def security_config(monkeypatch):
    """A known admin credential and cookie secret for every test; each test starts with a clean login limiter."""
    for name, value in (("ENV", "development"), ("COOKIE_SECRET", TEST_SECRET), ("ADMIN_PASSWORD_HASH", _TEST_HASH),
                        ("COOKIE_SECURE", None), ("SESSION_TTL_SECONDS", 3600), ("ALLOWED_ORIGINS", []),
                        ("DASH_PORT", 9000)):
        monkeypatch.setattr(config, name, value, raising=False)
    auth.login_limiter.failures.clear()


def session_cookie(clock=None):
    value = create_signed_value(TEST_SECRET, auth.SESSION_COOKIE, "admin", clock=clock)
    return auth.SESSION_COOKIE + "=" + value.decode()


@pytest_asyncio.fixture
async def db(monkeypatch, tmp_path):
    name = "observer_test_" + uuid.uuid4().hex[:12]
    monkeypatch.setattr(config, "MONGO_URL", TEST_MONGO_URL)
    monkeypatch.setattr(config, "DB_NAME", name)
    monkeypatch.setattr(config, "FILES_DIR", str(tmp_path / "files_buffer"))
    db_module.close_db()
    database = db_module.get_db()
    assert database.name.startswith("observer_test_")
    yield database
    assert database.name.startswith("observer_test_")
    await database.client.drop_database(database.name)
    db_module.close_db()


class Server:
    def __init__(self, base_url, stop):
        self.base_url = base_url
        self.stop = stop


@pytest_asyncio.fixture
async def serve(db):
    """Serve a Tornado application on an unused local port and return an httpx client for it."""
    servers = []
    clients = []

    def start(app, login=True, xsrf=True):
        """Client for `app`. By default it carries a valid admin session and XSRF token."""
        sock, port = bind_unused_port()
        server = HTTPServer(app)
        server.add_sockets([sock])
        servers.append(server)
        client = httpx.AsyncClient(base_url="http://127.0.0.1:%d" % port, timeout=30)
        cookies = []
        if xsrf:
            token = uuid.uuid4().hex
            cookies.append("_xsrf=" + token)
            client.headers["X-XSRFToken"] = token
        if login:
            cookies.append(session_cookie())
        if cookies:
            client.headers["Cookie"] = "; ".join(cookies)
        client.port = port
        clients.append(client)
        return client

    yield start
    for client in clients:
        await client.aclose()
    for server in servers:
        server.stop()


@pytest.fixture
def dash(serve):
    import root
    return serve(root.make_dash_app())


@pytest.fixture
def anon_dash(serve):
    """Dashboard client with an XSRF token but no session."""
    import root
    return serve(root.make_dash_app(), login=False)


def make_stat(name="box", cpu=None, ram=50.0, **overrides):
    """A record shaped like remote.get_stats() output."""
    record = {
        "date": "2026-01-01T00:00:00", "name": name, "cpu": cpu or [10.0, 20.0], "cpu_count": 2,
        "cpu_ctx_switches": 1, "cpu_interrupts": 1, "ram": ram, "ram-available": 1, "ram-used": 1,
        "swap": 0.0, "swap-total": 0, "swap-used": 0, "disk_io_read": 100, "disk_io_write": 200,
        "disk_total": 1000, "disk_used": 500, "uptime": 10.0, "packets_sent": [10, 0], "packets_recv": [20, 0],
        "bytes_recv": 1000, "bytes_sent": 2000, "dl_rate": 10, "ul_rate": 20, "disk_read_rate": 1,
        "disk_write_rate": 2,
    }
    record.update(overrides)
    return record
