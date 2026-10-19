import asyncio
import json
import time

import pytest

import admin_cli
import auth
import config
import root
from tests.conftest import TEST_PASSWORD, make_stat, session_cookie


async def api(client, **data):
    return await client.post("/api", data=data)


# --- password hashing --------------------------------------------------------------------------------------------

def test_hash_roundtrip_and_salting():
    first, second = auth.hash_password(TEST_PASSWORD), auth.hash_password(TEST_PASSWORD)
    assert first != second and first.startswith("scrypt:")
    assert TEST_PASSWORD not in first
    assert auth.verify_password(TEST_PASSWORD, first) and auth.verify_password(TEST_PASSWORD, second)
    assert not auth.verify_password(TEST_PASSWORD + "x", first)


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt:1:1:1:aa:bb", "scrypt:16384:8:1:!!:!!",
                                    "scrypt:999999999:8:1:aGk:aGk", "plaintext-password-value"])
def test_malformed_or_hostile_hashes_never_verify(stored):
    assert auth.verify_password(TEST_PASSWORD, stored) is False


def test_short_passwords_are_refused():
    with pytest.raises(ValueError):
        auth.hash_password("short")
    assert not auth.verify_password("short", auth.hash_password(TEST_PASSWORD))


# --- start-up validation -----------------------------------------------------------------------------------------

def test_production_refuses_to_start_without_credential(monkeypatch):
    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    with pytest.raises(auth.ConfigError, match="OBSERVER_ADMIN_PASSWORD_HASH"):
        auth.validate_config()


@pytest.mark.parametrize("value", ["not-a-hash", "hunter2hunter2hunter2", "scrypt:1:2"])
def test_production_refuses_a_plaintext_or_malformed_credential(monkeypatch, value):
    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", value)
    with pytest.raises(auth.ConfigError, match="not a valid hash"):
        auth.validate_config()


@pytest.mark.parametrize("secret", ["", "short", "a" * 40])
def test_weak_or_missing_cookie_secret_is_refused(monkeypatch, secret):
    monkeypatch.setattr(config, "COOKIE_SECRET", secret)
    with pytest.raises(auth.ConfigError, match="OBSERVER_COOKIE_SECRET"):
        auth.validate_config()


def test_unknown_environment_is_refused(monkeypatch):
    monkeypatch.setattr(config, "ENV", "prod")
    with pytest.raises(auth.ConfigError, match="OBSERVER_ENV"):
        auth.validate_config()


def test_valid_production_configuration_starts(monkeypatch):
    monkeypatch.setattr(config, "ENV", "production")
    auth.validate_config()
    assert auth.cookie_secure() is True


def test_development_without_credential_starts_but_has_no_login(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    auth.validate_config()
    assert auth.cookie_secure() is False


async def test_servers_validate_before_listening(monkeypatch):
    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    with pytest.raises(auth.ConfigError):
        await root._serve_http(root.make_live_app(), 0, "x")


# --- pages and static files --------------------------------------------------------------------------------------

@pytest.mark.parametrize("page", ["live", "stat", "perf", "cost", "admin"])
async def test_pages_redirect_to_login_without_a_session(anon_dash, page):
    response = await anon_dash.get("/" + page)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/login?next=")


async def test_login_page_and_assets_are_public(anon_dash):
    assert (await anon_dash.get("/login")).status_code == 200
    assert (await anon_dash.get("/css/styles.css")).status_code == 200
    assert (await anon_dash.get("/js/login.js")).status_code == 200


async def test_login_page_issues_an_xsrf_cookie(serve):
    client = serve(root.make_dash_app(), login=False, xsrf=False)
    response = await client.get("/login")
    assert "_xsrf" in response.headers.get("set-cookie", "")


async def test_downloads_need_a_session(serve, anon_dash):
    assert (await anon_dash.get("/files/anything.csv")).status_code == 401


async def test_next_parameter_cannot_redirect_off_site(anon_dash):
    for target in ("//evil.example", "https://evil.example", "/\\evil.example"):
        page = (await anon_dash.get("/login", params={"next": target})).text
        assert 'value="/live"' in page
    assert 'value="/stat"' in (await anon_dash.get("/login", params={"next": "/stat"})).text


# --- API requires a session --------------------------------------------------------------------------------------

DASH_ACTIONS = ["GET_REMOTE_MACHINES", "GET_SPECS", "GET_STAT_DATA", "FETCH_INSTANCE_PRICING", "SAVE_TEST",
                "FETCH_TESTS_LIST", "FETCH_TEST", "DELETE_REMOTE_MACHINE", "SAVE_TEST_RESULT_AS", "BOGUS"]


@pytest.mark.parametrize("action", DASH_ACTIONS)
async def test_every_api_action_is_refused_without_a_session(anon_dash, action):
    response = await api(anon_dash, action=action)
    assert response.status_code == 401


async def test_unauthenticated_delete_does_nothing(anon_dash, db):
    await db["machines"].insert_one({"name": "box"})
    await db["[box]-stat"].insert_one(make_stat())
    assert (await api(anon_dash, action="DELETE_REMOTE_MACHINE", machine_id="box")).status_code == 401
    assert await db["machines"].count_documents({}) == 1
    assert await db["[box]-stat"].count_documents({}) == 1


async def test_unauthenticated_save_test_stores_nothing(anon_dash, db):
    await api(anon_dash, action="SAVE_TEST", machine="box", testName="t", testCode="x")
    assert "[box]-tests" not in await db.list_collection_names()


@pytest.mark.parametrize("make,path,data", [
    (root.make_live_app, "/live", {"action": "GET_LIVE_DATA", "client-name": "box"}),
    (root.make_compute_app, "/compute", {"action": "GET_COST_STATS", "machine": "box"}),
    (root.make_sim_app, "/sim", {"action": "RUN_TEST"}),
])
async def test_services_refuse_requests_without_a_session(serve, make, path, data):
    client = serve(make(), login=False)
    assert (await client.post(path, data=data)).status_code == 401


async def test_live_data_is_not_readable_anonymously(serve, db):
    await db["[box]-live"].insert_one(make_stat())
    anon = serve(root.make_live_app(), login=False)
    response = await anon.post("/live", data={"action": "GET_LIVE_DATA", "client-name": "box"})
    assert response.status_code == 401 and "cpu" not in response.text


# --- login, sessions, logout -------------------------------------------------------------------------------------

async def test_login_success_sets_a_hardened_session_cookie(anon_dash):
    response = await api(anon_dash, action="LOGIN_ADMIN", password=TEST_PASSWORD)
    assert response.status_code == 200 and response.json() == {"auth": "True"}
    cookie = response.headers["set-cookie"].lower()
    assert auth.SESSION_COOKIE in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert TEST_PASSWORD not in response.text


async def test_session_from_login_unlocks_the_api(serve):
    client = serve(root.make_dash_app(), login=False)
    login = await api(client, action="LOGIN_ADMIN", password=TEST_PASSWORD)
    value = login.cookies[auth.SESSION_COOKIE]
    client.headers["Cookie"] = client.headers["Cookie"] + "; %s=%s" % (auth.SESSION_COOKIE, value)
    assert (await api(client, action="GET_REMOTE_MACHINES")).status_code == 200


@pytest.mark.parametrize("password", ["", "wrong password value", "short"])
async def test_login_failure_is_401_without_a_cookie(anon_dash, password):
    response = await api(anon_dash, action="LOGIN_ADMIN", password=password)
    assert response.status_code == 401 and response.json()["auth"] == "False"
    assert "set-cookie" not in response.headers


async def test_login_is_refused_when_no_credential_is_configured(anon_dash, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    assert (await api(anon_dash, action="LOGIN_ADMIN", password=TEST_PASSWORD)).status_code == 401


async def test_repeated_failures_are_rate_limited(anon_dash):
    for _ in range(5):
        assert (await api(anon_dash, action="LOGIN_ADMIN", password="wrong password value")).status_code == 401
    blocked = await api(anon_dash, action="LOGIN_ADMIN", password=TEST_PASSWORD)
    assert blocked.status_code == 429 and "retry-after" in blocked.headers


async def test_tampered_session_cookie_is_rejected(serve):
    client = serve(root.make_dash_app(), login=False)
    forged = session_cookie().replace("|", "|0", 1)
    client.headers["Cookie"] += "; " + forged
    assert (await api(client, action="GET_REMOTE_MACHINES")).status_code == 401


async def test_cookie_signed_with_another_secret_is_rejected(serve):
    from tornado.web import create_signed_value
    client = serve(root.make_dash_app(), login=False)
    other = create_signed_value("f" * 64, auth.SESSION_COOKIE, "admin").decode()
    client.headers["Cookie"] += "; %s=%s" % (auth.SESSION_COOKIE, other)
    assert (await api(client, action="GET_REMOTE_MACHINES")).status_code == 401


async def test_expired_session_is_rejected(serve):
    client = serve(root.make_dash_app(), login=False)
    old = session_cookie(clock=lambda: time.time() - 2 * config.SESSION_TTL_SECONDS)
    client.headers["Cookie"] += "; " + old
    assert (await api(client, action="GET_REMOTE_MACHINES")).status_code == 401


async def test_logout_clears_the_cookie(dash):
    response = await api(dash, action="LOGOUT_ADMIN")
    assert response.status_code == 200 and auth.SESSION_COOKIE in response.headers["set-cookie"]


# --- XSRF --------------------------------------------------------------------------------------------------------

async def test_post_without_xsrf_token_is_forbidden_even_when_logged_in(serve):
    client = serve(root.make_dash_app(), xsrf=False)
    assert (await api(client, action="GET_REMOTE_MACHINES")).status_code == 403


async def test_post_with_mismatched_xsrf_token_is_forbidden(dash):
    dash.headers["X-XSRFToken"] = "0" * 32
    assert (await api(dash, action="GET_REMOTE_MACHINES")).status_code == 403


async def test_state_changing_service_calls_also_need_the_token(serve):
    client = serve(root.make_sim_app(), xsrf=False)
    assert (await client.post("/sim", data={"action": "RUN_TEST"})).status_code == 403


# --- CORS --------------------------------------------------------------------------------------------------------

async def test_cors_allows_only_the_dashboard_origin_with_credentials(serve, monkeypatch):
    client = serve(root.make_live_app())
    monkeypatch.setattr(config, "DASH_PORT", 9000)
    good = await client.post("/live", data={"action": "NONE"}, headers={"Origin": "http://127.0.0.1:9000"})
    assert good.headers["access-control-allow-origin"] == "http://127.0.0.1:9000"
    assert good.headers["access-control-allow-credentials"] == "true"
    for origin in ("http://evil.example", "http://127.0.0.1:9999", "null", "http://127.0.0.1.evil.example:9000"):
        bad = await client.post("/live", data={"action": "NONE"}, headers={"Origin": origin})
        assert "access-control-allow-origin" not in bad.headers


async def test_cors_never_uses_a_wildcard(dash):
    for origin in ("*", "http://anywhere.example"):
        response = await api(dash, action="GET_REMOTE_MACHINES")
        assert response.headers.get("access-control-allow-origin") != "*"
        response = await dash.post("/api", data={"action": "GET_REMOTE_MACHINES"}, headers={"Origin": origin})
        assert response.headers.get("access-control-allow-origin") != "*"


async def test_configured_extra_origin_is_allowed(serve, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", ["https://observer.example.org"])
    client = serve(root.make_live_app())
    response = await client.post("/live", data={"action": "NONE"}, headers={"Origin": "https://observer.example.org"})
    assert response.headers["access-control-allow-origin"] == "https://observer.example.org"


async def test_preflight_lists_the_xsrf_header_and_needs_no_session(serve):
    client = serve(root.make_live_app(), login=False, xsrf=False)
    response = await client.options("/live", headers={"Origin": "http://127.0.0.1:9000",
                                                      "Access-Control-Request-Method": "POST"})
    assert response.status_code == 204
    assert "x-xsrftoken" in response.headers["access-control-allow-headers"].lower()


# --- agent tokens over TCP ---------------------------------------------------------------------------------------

SPECS = {"machine": "m", "node": "n", "architecture": ["64bit", ""], "system": "Linux", "release": "r",
         "version": "v", "name": "box", "memory": 1}


async def talk(port, lines, wait=0.4):
    """Send raw lines, then report whether the server closed the connection."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    for line in lines:
        writer.write(line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode())
    await writer.drain()
    await asyncio.sleep(wait)
    try:
        closed = await asyncio.wait_for(reader.read(1), 2) == b""
    except asyncio.TimeoutError:
        closed = False
    writer.close()
    return closed


@pytest.fixture
async def listener(db):
    from tornado.testing import bind_unused_port
    sock, port = bind_unused_port()
    server = root.TCPListener()
    server.add_sockets([sock])
    yield port
    server.stop()


async def test_valid_token_is_accepted(db, listener):
    token = await auth.create_agent("box")
    await talk(listener, [{"token": token}, SPECS, make_stat()])
    assert await db["machines"].count_documents({"name": "box"}) == 1
    assert await db["[box]-live"].count_documents({}) == 1


@pytest.mark.parametrize("first", [None, {"hello": "world"}, {"token": "nope"}, {"token": ""}, {"token": 5},
                                   [1, 2], b"not json\n"])
async def test_missing_unknown_or_wrong_token_is_rejected(db, listener, first):
    await auth.create_agent("box")
    lines = ([first] if first is not None else []) + [SPECS, make_stat()]
    assert await talk(listener, lines) is True
    assert await db["machines"].count_documents({}) == 0
    assert "[box]-live" not in await db.list_collection_names()


async def test_token_belongs_to_one_name_only(db, listener):
    token = await auth.create_agent("box")
    other = dict(make_stat(), name="other")
    assert await talk(listener, [{"token": token}, other]) is True
    assert "[other]-live" not in await db.list_collection_names()


async def test_oversized_line_closes_the_connection(db, listener):
    token = await auth.create_agent("box")
    assert await talk(listener, [{"token": token}, b"x" * (root.MAX_LINE_BYTES + 10) + b"\n"]) is True


async def test_malformed_record_closes_only_that_connection(db, listener):
    token = await auth.create_agent("box")
    assert await talk(listener, [{"token": token}, b"{not json}\n"]) is True
    await talk(listener, [{"token": token}, SPECS])
    assert await db["machines"].count_documents({"name": "box"}) == 1


async def test_revoked_agent_is_disconnected(db, listener, monkeypatch):
    monkeypatch.setattr(root, "AGENT_RECHECK_SECONDS", 0)
    token = await auth.create_agent("box")
    reader, writer = await asyncio.open_connection("127.0.0.1", listener)
    writer.write((json.dumps({"token": token}) + "\n" + json.dumps(SPECS) + "\n").encode())
    await writer.drain()
    await asyncio.sleep(0.3)
    assert await auth.revoke_agent("box")
    writer.write((json.dumps(make_stat()) + "\n").encode())
    await writer.drain()
    await asyncio.sleep(0.3)
    assert await asyncio.wait_for(reader.read(1), 2) == b""
    assert await db["[box]-live"].count_documents({}) == 0
    writer.close()


async def test_silent_connection_times_out(db, listener, monkeypatch):
    monkeypatch.setattr(root, "AUTH_TIMEOUT_SECONDS", 0.2)
    assert await talk(listener, [], wait=0.6) is True


# --- agent administration ----------------------------------------------------------------------------------------

async def test_token_is_stored_only_as_a_digest(db):
    token = await auth.create_agent("box")
    doc = await db["agents"].find_one({"name": "box"})
    assert token not in json.dumps({k: str(v) for k, v in doc.items()})
    assert doc["token_sha256"] == auth.token_digest(token) and len(token) >= 40


async def test_agent_names_are_unique_and_validated(db):
    await auth.create_agent("box")
    with pytest.raises(ValueError):
        await auth.create_agent("box")
    for bad in ("", "has space", "a/b", "<b>x</b>", "x" * 65):
        with pytest.raises(ValueError):
            await auth.create_agent(bad)


async def test_revoke_and_list(db):
    token = await auth.create_agent("box")
    assert await auth.list_agents() == ["box"]
    assert await auth.revoke_agent("box") and not await auth.revoke_agent("box")
    assert await auth.find_agent(token) is None


def test_hash_password_command_refuses_mismatch_and_short(monkeypatch, capsys):
    answers = iter(["a very long first password", "something else entirely"])
    monkeypatch.setattr(admin_cli.getpass, "getpass", lambda prompt="": next(answers))
    assert admin_cli.hash_password_command() == 1
    answers = iter(["short", "short"])
    assert admin_cli.hash_password_command() == 1


def test_hash_password_command_prints_a_verifiable_hash(monkeypatch, capsys):
    monkeypatch.setattr(admin_cli.getpass, "getpass", lambda prompt="": TEST_PASSWORD)
    assert admin_cli.hash_password_command() == 0
    assert auth.verify_password(TEST_PASSWORD, capsys.readouterr().out.strip())


def test_run_py_exits_non_zero_when_production_config_is_invalid(monkeypatch, capsys):
    import run
    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    assert run.main(["run.py", "live", "0"]) == 1
    assert "Refusing to start" in capsys.readouterr().err


