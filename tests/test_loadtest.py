import asyncio
import ast
import json
import os
import pathlib
import time
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from bson import ObjectId
from tornado.httpserver import HTTPServer
from tornado.testing import bind_unused_port
from tornado.web import Application, RequestHandler

import config
import db as db_module
import loadtest

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def loadtest_config(monkeypatch):
    monkeypatch.setattr(config, "LOADTEST_ALLOWED_HOSTS", ["127.0.0.1", "*.example.org", "10.0.0.0/8"])
    monkeypatch.setattr(config, "LOADTEST_MAX_CONCURRENCY", 10)
    monkeypatch.setattr(config, "LOADTEST_MAX_DURATION", 120)
    monkeypatch.setattr(config, "LOADTEST_PAGE_TIMEOUT", 10.0)


def raw(url="http://127.0.0.1:8888/", concurrency=3, duration=20):
    return {"url": url, "concurrency": concurrency, "duration": duration}


# --- parameters and targets -------------------------------------------------------------------------------------

def test_valid_parameters_are_parsed():
    params = loadtest.parse_params(raw(" http://127.0.0.1:8888/healthz ", "4", "30"))
    assert params == loadtest.Params("http://127.0.0.1:8888/healthz", 4, 30)
    assert params.as_dict() == {"url": "http://127.0.0.1:8888/healthz", "concurrency": 4, "duration": 30}


@pytest.mark.parametrize("extra", [{"script": "alert(1)"}, {"code": "x"}, {"steps": []}, {"headers": {"a": "b"}},
                                   {"method": "POST"}, {"body": "x"}])
def test_there_is_no_way_to_pass_code_or_extra_options(extra):
    with pytest.raises(loadtest.LoadtestError, match="unknown parameter"):
        loadtest.parse_params(dict(raw(), **extra))


@pytest.mark.parametrize("bad", [None, [], "x", 5])
def test_parameters_must_be_an_object(bad):
    with pytest.raises(loadtest.LoadtestError):
        loadtest.parse_params(bad)


@pytest.mark.parametrize("missing", ["url", "concurrency", "duration"])
def test_every_parameter_is_required(missing):
    data = raw()
    del data[missing]
    with pytest.raises(loadtest.LoadtestError, match="missing"):
        loadtest.parse_params(data)


@pytest.mark.parametrize("concurrency", [0, -1, 11, "many", None, True, 2.5 * 10 ** 9, ""])
def test_concurrency_is_a_bounded_whole_number(concurrency):
    with pytest.raises(loadtest.LoadtestError):
        loadtest.parse_params(raw(concurrency=concurrency))


@pytest.mark.parametrize("duration", [0, 4, 121, "long", None, True, ""])
def test_duration_is_bounded(duration):
    with pytest.raises(loadtest.LoadtestError):
        loadtest.parse_params(raw(duration=duration))


def test_limits_follow_the_configuration(monkeypatch):
    monkeypatch.setattr(config, "LOADTEST_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(config, "LOADTEST_MAX_DURATION", 10)
    loadtest.parse_params(raw(concurrency=2, duration=10))
    with pytest.raises(loadtest.LoadtestError):
        loadtest.parse_params(raw(concurrency=3))
    with pytest.raises(loadtest.LoadtestError):
        loadtest.parse_params(raw(duration=11))


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/", "file:///etc/passwd", "javascript:alert(1)", "data:text/html,x",
                                 "http://", "//127.0.0.1/", "127.0.0.1", "", "http://user:pw@127.0.0.1/",
                                 "http://127.0.0.1:99999/", "http://127.0.0.1/" + "a" * 2100, 5])
def test_only_plain_http_urls_without_credentials(url):
    with pytest.raises(loadtest.LoadtestError):
        loadtest.parse_params(raw(url=url))


@pytest.mark.parametrize("url", ["http://evil.example.com/", "http://localhost:8888/", "http://192.168.1.5/",
                                 "http://example.org/", "http://127.0.0.2/", "http://notexample.org/"])
def test_targets_must_be_on_the_allow_list(url):
    with pytest.raises(loadtest.LoadtestError, match="ALLOWED_HOSTS"):
        loadtest.parse_params(raw(url=url))


@pytest.mark.parametrize("url", ["http://127.0.0.1:8888/", "https://a.example.org/x?y=1", "http://B.EXAMPLE.ORG/",
                                 "http://10.20.30.40/", "http://127.0.0.1./"])
def test_allowed_targets(url):
    assert loadtest.parse_params(raw(url=url)).url == url


def test_empty_allow_list_allows_nothing(monkeypatch):
    monkeypatch.setattr(config, "LOADTEST_ALLOWED_HOSTS", [])
    with pytest.raises(loadtest.LoadtestError):
        loadtest.parse_params(raw())


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/",
                                 "http://[fe80::1]/", "http://169.254.0.1/", "http://instance-data/"])
def test_metadata_and_link_local_addresses_are_never_allowed_even_if_listed(url, monkeypatch):
    listed = ["169.254.169.254", "169.254.0.0/16", "metadata.google.internal", "fe80::1", "instance-data", "*.internal"]
    monkeypatch.setattr(config, "LOADTEST_ALLOWED_HOSTS", listed)
    with pytest.raises(loadtest.LoadtestError):
        loadtest.parse_params(raw(url=url))


def test_host_allowed_rules():
    allowed = ["Example.ORG", "*.example.net", "192.168.0.0/16", "::1"]
    assert loadtest.host_allowed("example.org", allowed)
    assert loadtest.host_allowed("a.b.example.net", allowed)
    assert not loadtest.host_allowed("example.net", allowed)           # the wildcard needs a label
    assert loadtest.host_allowed("192.168.4.4", allowed) and not loadtest.host_allowed("192.169.0.1", allowed)
    assert loadtest.host_allowed("::1", allowed) and not loadtest.host_allowed("", allowed)
    assert not loadtest.host_allowed("evil.example.org.attacker.io", ["*.example.org"])


# --- statistics ---------------------------------------------------------------------------------------------------

def test_percentiles_use_nearest_rank():
    values = list(range(1, 101))
    assert loadtest.percentile(values, 50) == 50 and loadtest.percentile(values, 95) == 95
    assert loadtest.percentile([7], 95) == 7 and loadtest.percentile([], 50) is None


def test_summary_and_result_shape():
    samples = [{"ok": True, "status": 200, "wall_ms": 10, "ttfb_ms": 2, "dom_content_loaded_ms": 8, "load_ms": 9,
                "transfer_bytes": 100, "resources": 1, "resource_bytes": 50},
               {"ok": True, "status": 200, "wall_ms": 30, "ttfb_ms": 4, "dom_content_loaded_ms": 20, "load_ms": 25,
                "transfer_bytes": 100, "resources": 1, "resource_bytes": 50},
               {"ok": False, "status": 503, "wall_ms": 99}]
    errors = {"timeout": 0, "network": 0, "http_error": 1, "other": 0}
    result = loadtest.build_result(samples, errors, 2, {"cdn.x"}, 4.0)
    assert result["loads"] == 3 and result["ok_loads"] == 2 and result["throughput_per_s"] == 0.75
    assert result["status_codes"] == {"200": 2, "503": 1}
    assert result["summary"]["wall_ms"] == {"count": 2, "min": 10, "mean": 20.0, "p50": 10, "p95": 30, "max": 30}
    assert result["blocked_requests"] == 2 and result["blocked_hosts"] == ["cdn.x"]
    assert loadtest.summarise([]) is None


# --- API ----------------------------------------------------------------------------------------------------------

async def api(client, action, **data):
    return await client.post("/api", data=dict(action=action, **data))


@pytest.mark.parametrize("action", ["START_LOADTEST", "GET_LOADTESTS", "GET_LOADTEST", "CANCEL_LOADTEST"])
async def test_load_test_actions_need_a_session(anon_dash, action):
    assert (await api(anon_dash, action)).status_code == 401


async def test_starting_a_test_needs_the_xsrf_token(serve):
    import root
    client = serve(root.make_dash_app(), xsrf=False)
    assert (await api(client, "START_LOADTEST", **raw())).status_code == 403


async def test_start_queues_a_run_and_it_is_listed(dash, db):
    started = (await api(dash, "START_LOADTEST", **raw())).json()
    run = (await api(dash, "GET_LOADTEST", id=started["id"])).json()
    assert run["status"] == "queued" and run["params"] == {"url": "http://127.0.0.1:8888/", "concurrency": 3,
                                                           "duration": 20}
    listing = (await api(dash, "GET_LOADTESTS")).json()
    assert [r["id"] for r in listing["runs"]] == [started["id"]]
    assert listing["limits"] == {"max_concurrency": 10, "max_duration": 120}
    assert "127.0.0.1" in listing["allowed_hosts"] and listing["worker"]["running"] is False


@pytest.mark.parametrize("extra", [{"script": "x"}, {"code": "y"}, {"steps": "z"}, {"url2": "http://127.0.0.1/"}])
async def test_extra_request_arguments_are_refused(dash, db, extra):
    response = await api(dash, "START_LOADTEST", **raw(), **extra)
    assert response.status_code == 400 and "only url, concurrency and duration" in response.json()["message"]
    assert await db["loadtests"].count_documents({}) == 0


@pytest.mark.parametrize("data,message", [
    (raw(url="http://evil.example.com/"), "ALLOWED_HOSTS"), (raw(concurrency=99), "concurrency"),
    (raw(duration=1), "duration"), (raw(url="file:///etc/passwd"), "http://"), (raw(url=""), "http://")])
async def test_invalid_requests_are_400_and_store_nothing(dash, db, data, message):
    response = await api(dash, "START_LOADTEST", **data)
    assert response.status_code == 400 and message in response.json()["message"]
    assert await db["loadtests"].count_documents({}) == 0


async def test_the_queue_is_bounded(dash, db):
    for _ in range(loadtest.MAX_QUEUE):
        assert (await api(dash, "START_LOADTEST", **raw())).status_code == 200
    response = await api(dash, "START_LOADTEST", **raw())
    assert response.status_code == 400 and "too many tests" in response.json()["message"]


async def test_unknown_or_malformed_ids_are_404(dash, db):
    for call in ("GET_LOADTEST", "CANCEL_LOADTEST"):
        for bad in ("nope", "", str(ObjectId())):
            assert (await api(dash, call, id=bad)).status_code == 404


async def test_cancel_marks_a_waiting_run(dash, db):
    run_id = (await api(dash, "START_LOADTEST", **raw())).json()["id"]
    assert (await api(dash, "CANCEL_LOADTEST", id=run_id)).json() == {"status": "cancelling"}
    assert (await db["loadtests"].find_one({"_id": ObjectId(run_id)}))["cancel_requested"] is True


async def test_listing_leaves_out_the_samples(dash, db):
    await db["loadtests"].insert_one({"status": "done", "params": raw(), "created": time.time(),
                                      "samples": [{"a": 1}] * 5, "result": {"loads": 5}})
    run = (await api(dash, "GET_LOADTESTS")).json()["runs"][0]
    assert "samples" not in run and run["result"]["loads"] == 5


# --- worker (fake runner) -------------------------------------------------------------------------------------------

def make_worker(runner, **kw):
    return loadtest.Worker(runner=runner, **kw)


async def queue_run(db, **overrides):
    return (await db["loadtests"].insert_one(dict({"status": "queued", "params": raw(), "created": time.time()},
                                                  **overrides))).inserted_id


async def test_worker_runs_a_queued_test_and_stores_the_result(db):
    async def runner(params, stop=None, progress=None):
        await progress({"elapsed": 1.0, "loads": 2, "errors": 0})
        return {"loads": 2, "cancelled": False}, [{"t": 0}]

    run_id = await queue_run(db)
    worker = make_worker(runner)
    doc = await worker.claim()
    assert doc["_id"] == run_id and doc["status"] == "running"
    await worker.execute(doc)
    stored = await db["loadtests"].find_one({"_id": run_id})
    assert stored["status"] == "done" and stored["result"]["loads"] == 2 and stored["samples"] == [{"t": 0}]
    assert stored["progress"] == {"elapsed": 1.0, "loads": 2, "errors": 0}


async def test_runs_are_claimed_oldest_first_and_once(db):
    first = await queue_run(db, created=1.0)
    second = await queue_run(db, created=2.0)
    worker = make_worker(None)
    assert (await worker.claim())["_id"] == first and (await worker.claim())["_id"] == second
    assert await worker.claim() is None


async def test_a_failing_runner_stores_a_generic_error_without_internals(db):
    async def runner(params, stop=None, progress=None):
        raise RuntimeError("/secret/path/stack detail")

    run_id = await queue_run(db)
    worker = make_worker(runner)
    await worker.execute(await worker.claim())
    stored = await db["loadtests"].find_one({"_id": run_id})
    assert stored["status"] == "failed" and stored["error"] == "the test could not be run"
    assert "secret" not in json.dumps(stored, default=str)


async def test_a_refused_target_is_reported_as_failed(db):
    async def runner(params, stop=None, progress=None):
        raise loadtest.LoadtestError("the host resolves to a blocked address")

    run_id = await queue_run(db)
    worker = make_worker(runner)
    await worker.execute(await worker.claim())
    assert (await db["loadtests"].find_one({"_id": run_id}))["error"] == "the host resolves to a blocked address"


async def test_the_allow_list_is_checked_again_when_the_run_starts(db, monkeypatch):
    called = []

    async def runner(params, stop=None, progress=None):
        called.append(params)
        return {"loads": 0}, []

    run_id = await queue_run(db)
    monkeypatch.setattr(config, "LOADTEST_ALLOWED_HOSTS", ["somewhere.else"])      # changed after queuing
    worker = make_worker(runner)
    await worker.execute(await worker.claim())
    stored = await db["loadtests"].find_one({"_id": run_id})
    assert stored["status"] == "failed" and "ALLOWED_HOSTS" in stored["error"] and called == []


async def test_a_tampered_stored_run_is_not_executed(db):
    called = []

    async def runner(params, stop=None, progress=None):
        called.append(params)
        return {"loads": 0}, []

    run_id = await queue_run(db, params={"url": "http://127.0.0.1/", "concurrency": 3, "duration": 20,
                                         "script": "alert(1)"})
    worker = make_worker(runner)
    await worker.execute(await worker.claim())
    assert (await db["loadtests"].find_one({"_id": run_id}))["status"] == "failed" and called == []


async def test_cancel_before_the_run_starts(db):
    async def runner(params, stop=None, progress=None):
        raise AssertionError("must not run")

    run_id = await queue_run(db, cancel_requested=True)
    worker = make_worker(runner)
    await worker.execute(await worker.claim())
    assert (await db["loadtests"].find_one({"_id": run_id}))["status"] == "cancelled"


async def test_cancel_during_the_run_stops_it(db):
    seen = {}

    async def runner(params, stop=None, progress=None):
        for _ in range(100):
            await progress({"elapsed": 0, "loads": 0, "errors": 0})
            if stop.is_set():
                seen["stopped"] = True
                return {"loads": 1, "cancelled": True}, []
            await asyncio.sleep(0.05)
        return {"loads": 1, "cancelled": False}, []

    run_id = await queue_run(db)
    worker = make_worker(runner)
    doc = await worker.claim()
    task = asyncio.create_task(worker.execute(doc))
    await asyncio.sleep(0.3)
    await db["loadtests"].update_one({"_id": run_id}, {"$set": {"cancel_requested": True}})
    await asyncio.wait_for(task, 10)
    assert seen == {"stopped": True} and (await db["loadtests"].find_one({"_id": run_id}))["status"] == "cancelled"


async def test_a_run_left_running_by_a_dead_worker_is_failed(db):
    run_id = await queue_run(db, status="running", worker="someone-else")
    await make_worker(None).fail_stale()
    stored = await db["loadtests"].find_one({"_id": run_id})
    assert stored["status"] == "failed" and "stopped" in stored["error"]


async def test_only_one_worker_holds_the_lease(db):
    clock = [1000.0]
    first, second = make_worker(None, clock=lambda: clock[0], holder="a"), make_worker(None, clock=lambda: clock[0],
                                                                                       holder="b")
    assert await first.acquire_lease() and not await second.acquire_lease()
    clock[0] += loadtest.LEASE_SECONDS + 1
    assert await second.acquire_lease() and not await first.acquire_lease()


async def test_worker_loop_runs_queued_tests_and_reports_status(db, monkeypatch):
    async def runner(params, stop=None, progress=None):
        return {"loads": 1, "cancelled": False}, []

    run_id = await queue_run(db)
    worker = make_worker(runner)
    task = asyncio.create_task(worker.run())
    for _ in range(100):
        if (await db["loadtests"].find_one({"_id": run_id}))["status"] == "done":
            break
        await asyncio.sleep(0.1)
    status = await loadtest.worker_status()
    worker.stop_event.set()
    await asyncio.wait_for(task, 10)
    assert (await db["loadtests"].find_one({"_id": run_id}))["status"] == "done" and status["running"] is True


async def test_load_test_collection_gets_a_ttl_index(db, monkeypatch):
    monkeypatch.setattr(config, "LOADTEST_RETENTION_DAYS", 5)
    await db_module.ensure_indexes()
    indexes = {s["name"]: s async for s in await db["loadtests"].list_indexes()}
    assert indexes["ts_1"]["expireAfterSeconds"] == 5 * 86400


# --- real browser runs against local servers --------------------------------------------------------------------

class Stats:
    def __init__(self):
        self.active = self.peak = self.hits = 0


class Page(RequestHandler):
    stats = None

    async def get(self):
        stats = self.application.stats
        stats.hits += 1
        stats.active += 1
        stats.peak = max(stats.peak, stats.active)
        try:
            await asyncio.sleep(0.15)
            self.write("<html><body><h1>page</h1><img src='/pixel.gif'></body></html>")
        finally:
            stats.active -= 1


class Pixel(RequestHandler):
    def get(self):
        self.set_header("Content-Type", "image/gif")
        self.write(b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
                   b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


class Boom(RequestHandler):
    def get(self):
        self.set_status(500)
        self.write("broken")


class Slow(RequestHandler):
    async def get(self):
        await asyncio.sleep(5)
        self.write("late")


class Mixed(RequestHandler):
    def get(self):
        other = self.application.other_port
        self.write("<html><body>x<img src='http://localhost:%d/tracker.gif'></body></html>" % other)


class Bounce(RequestHandler):
    def get(self):
        self.redirect("http://localhost:%d/landed" % self.application.other_port)


class Home(RequestHandler):
    def get(self):
        self.redirect("/")


class Other(RequestHandler):
    def get(self, *args):
        self.application.stats.hits += 1
        self.write("other")


def _app(handlers):
    app = Application(handlers)
    app.stats = Stats()
    return app


@pytest_asyncio.fixture
async def site():
    other = _app([(r"/(.*)", Other)])
    sock, other_port = bind_unused_port()
    other_server = HTTPServer(other)
    other_server.add_sockets([sock])
    main = _app([(r"/", Page), (r"/pixel.gif", Pixel), (r"/boom", Boom), (r"/slow", Slow), (r"/mixed", Mixed),
                 (r"/bounce", Bounce), (r"/home", Home)])
    main.other_port = other_port
    sock, port = bind_unused_port()
    server = HTTPServer(main)
    server.add_sockets([sock])
    yield SiteInfo(main, other, port, other_port)
    server.stop()
    other_server.stop()


class SiteInfo:
    def __init__(self, main, other, port, other_port):
        self.main, self.other, self.port, self.other_port = main, other, port, other_port

    def url(self, path="/"):
        return "http://127.0.0.1:%d%s" % (self.port, path)


def chromium_ready():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001
        return False


needs_browser = pytest.mark.skipif(not chromium_ready(), reason="Chromium for Playwright is not installed")


async def run(url, concurrency=2, duration=3, allowed=("127.0.0.1",), stop=None):
    params = loadtest.Params(url, concurrency, duration)
    return await loadtest.run_loadtest(params, allowed=list(allowed), stop=stop)


@needs_browser
async def test_a_real_run_measures_page_loads(site):
    result, samples = await run(site.url(), concurrency=2, duration=3)
    assert result["loads"] >= 4 and result["ok_loads"] == result["loads"]
    assert result["status_codes"] == {"200": result["loads"]}
    assert sum(result["errors"].values()) == 0 and result["blocked_requests"] == 0
    for metric in loadtest.METRICS:
        assert result["summary"][metric]["count"] == result["ok_loads"]
    assert result["summary"]["load_ms"]["min"] >= result["summary"]["ttfb_ms"]["min"] > 0
    assert result["summary"]["wall_ms"]["min"] >= 150          # the server takes 150 ms
    assert result["summary"]["resources"]["min"] == 1          # the image
    assert 3 <= result["elapsed_s"] < 8 and result["throughput_per_s"] > 0
    assert {s["user"] for s in samples} == {0, 1} and samples[0]["t"] >= 0


@needs_browser
async def test_virtual_users_load_pages_at_the_same_time(site):
    await run(site.url(), concurrency=3, duration=3)
    assert 2 <= site.main.stats.peak <= 3


@needs_browser
async def test_one_user_never_overlaps_itself(site):
    await run(site.url(), concurrency=1, duration=2)
    assert site.main.stats.peak == 1


@needs_browser
async def test_every_load_is_a_fresh_load(site):
    result, _ = await run(site.url(), concurrency=1, duration=3)
    assert site.main.stats.hits == result["loads"]            # no cached documents


@needs_browser
async def test_http_errors_are_counted_not_averaged_in(site):
    result, _ = await run(site.url("/boom"), concurrency=1, duration=2)
    assert result["ok_loads"] == 0 and result["errors"]["http_error"] == result["loads"] > 0
    assert result["status_codes"] == {"500": result["loads"]} and result["summary"]["wall_ms"] is None


@needs_browser
async def test_connection_failures_are_network_errors():
    sock, port = bind_unused_port()
    sock.close()
    result, _ = await run("http://127.0.0.1:%d/" % port, concurrency=1, duration=2)
    assert result["errors"]["network"] == result["loads"] > 0 and result["ok_loads"] == 0


@needs_browser
async def test_slow_pages_time_out(site, monkeypatch):
    monkeypatch.setattr(config, "LOADTEST_PAGE_TIMEOUT", 1.0)
    result, _ = await run(site.url("/slow"), concurrency=1, duration=2)
    assert result["errors"]["timeout"] >= 1 and result["ok_loads"] == 0


@needs_browser
async def test_requests_to_hosts_that_are_not_allowed_are_blocked_and_never_sent(site):
    result, _ = await run(site.url("/mixed"), concurrency=1, duration=2)       # allowed: 127.0.0.1 only
    assert result["ok_loads"] > 0 and result["blocked_requests"] >= 1 and "localhost" in result["blocked_hosts"]
    assert site.other.stats.hits == 0


@needs_browser
async def test_redirects_to_other_hosts_are_blocked_too(site):
    result, _ = await run(site.url("/bounce"), concurrency=1, duration=2)
    assert result["ok_loads"] == 0 and result["blocked_requests"] >= 1 and site.other.stats.hits == 0


@needs_browser
async def test_redirects_inside_the_allowed_hosts_are_followed(site):
    result, _ = await run(site.url("/home"), concurrency=1, duration=2)
    assert result["ok_loads"] == result["loads"] > 0 and result["blocked_requests"] == 0
    assert result["status_codes"] == {"200": result["loads"]}


@needs_browser
async def test_allowing_the_other_host_lets_its_requests_through(site):
    result, _ = await run(site.url("/mixed"), concurrency=1, duration=2, allowed=("127.0.0.1", "localhost"))
    assert result["blocked_requests"] == 0


@needs_browser
async def test_a_run_can_be_stopped_early(site):
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(1.0, stop.set)
    started = time.monotonic()
    result, _ = await run(site.url(), concurrency=2, duration=30, stop=stop)
    assert result["cancelled"] is True and time.monotonic() - started < 10


@needs_browser
async def test_progress_is_reported_while_running(site):
    seen = []

    async def progress(info):
        seen.append(info)

    params = loadtest.Params(site.url(), 1, 3)
    await loadtest.run_loadtest(params, allowed=["127.0.0.1"], progress=progress)
    assert seen and seen[-1]["loads"] >= seen[0]["loads"] and set(seen[0]) == {"elapsed", "loads", "errors"}


async def test_a_host_that_resolves_to_a_blocked_address_is_refused(monkeypatch):
    monkeypatch.setattr(loadtest, "resolves_to_blocked", lambda host: True)
    with pytest.raises(loadtest.LoadtestError, match="blocked address"):
        await run("http://127.0.0.1:1/")


@needs_browser
async def test_the_whole_chain_api_worker_browser_result(dash, db, site, monkeypatch):
    monkeypatch.setattr(config, "LOADTEST_ALLOWED_HOSTS", ["127.0.0.1"])
    run_id = (await api(dash, "START_LOADTEST", url=site.url(), concurrency=2, duration=5)).json()["id"]
    worker = loadtest.Worker()
    task = asyncio.create_task(worker.run())
    for _ in range(200):
        run = (await api(dash, "GET_LOADTEST", id=run_id)).json()
        if run["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.2)
    worker.stop_event.set()
    await asyncio.wait_for(task, 10)
    assert run["status"] == "done", run
    assert run["result"]["loads"] >= 4 and run["result"]["summary"]["load_ms"]["p95"] > 0 and run["samples"]
    assert run["params"]["duration"] == 5 and 5 <= run["result"]["elapsed_s"] < 12


# --- page, script, packaging -----------------------------------------------------------------------------------------

def test_the_page_script_runs_no_dynamic_code_and_builds_rows_with_text():
    script = (ROOT / "static" / "js" / "perf.js").read_text()
    for forbidden in ("eval(", "new Function", ".html(", "innerHTML", "document.write", "setTimeout(\""):
        assert forbidden not in script


def test_the_only_script_the_browser_runs_is_the_fixed_timing_reader():
    source = (ROOT / "loadtest.py").read_text()
    assert source.count("page.evaluate(") == 1 and "page.evaluate(TIMING_JS)" in source
    tree = ast.parse(source)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") in ("exec", "eval")]
    assert "params.url" in source and "script" not in loadtest.PARAM_KEYS


def test_the_loadtest_image_copies_every_module_it_needs():
    local = {p.stem for p in ROOT.glob("*.py")}
    needed, todo = set(), ["run", "root", "admin_cli", "loadtest", "agent"]
    while todo:
        name = todo.pop()
        if name in needed:
            continue
        needed.add(name)
        for node in ast.walk(ast.parse((ROOT / (name + ".py")).read_text())):
            if isinstance(node, ast.Import):
                modules = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module.split(".")[0]]
            else:
                modules = []
            todo.extend(m for m in modules if m in local)
    lines = (ROOT / "Dockerfile.loadtest").read_text().splitlines()
    copy_line = next(line for line in lines if line.startswith("COPY config"))
    assert needed <= {os.path.splitext(n)[0] for n in copy_line.split()[1:-1]}


def test_ci_installs_the_browser_and_builds_the_loadtest_image():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "playwright install --with-deps chromium" in ci and "--profile loadtest build loadtest" in ci


def test_run_py_has_a_loadtest_role(monkeypatch):
    import run
    monkeypatch.setattr(loadtest, "main", lambda: 0)
    assert run.main(["run.py", "loadtest"]) == 0


def test_urls_in_results_are_only_ever_the_requested_one():
    assert urlsplit(loadtest.parse_params(raw()).url).hostname == "127.0.0.1"
