import asyncio
import hashlib
import hmac
import json
import logging
import pathlib

import pytest
import pytest_asyncio
from tornado.httpserver import HTTPServer
from tornado.testing import bind_unused_port
from tornado.web import Application, RequestHandler

import agent
import auth
import config
import db as db_module
import monitor
import root
from tests.conftest import make_stat

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Capture:
    """Stands in for the webhook transport and records what would have been sent."""

    def __init__(self, fail_first=0):
        self.bodies, self.headers, self.fail_first, self.calls = [], [], fail_first, 0

    async def __call__(self, body, headers):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise OSError("webhook answered HTTP 500")
        self.bodies.append(json.loads(body))
        self.headers.append((headers, body))


def make_monitor(clock, probes=(), runner=None, capture=None, **kwargs):
    capture = capture or Capture()
    notifier = monitor.Notifier("http://hook.invalid/x", kwargs.pop("secret", ""), retry_delays=(0.01,),
                                post=capture)
    return monitor.Monitor(probes, notifier, clock=clock, runner=runner or monitor.run_probe, down_after=15,
                           **kwargs), capture


async def add_machine(db, name="box", last_online=1_000_000, interval=None):
    await db["machines"].insert_one({"name": name, "ip": "10.0.0.1", "last_online": str(int(last_online)),
                                     "interval": interval})


async def state_of(db, key):
    return await db["states"].find_one({"_id": key})


async def events(db):
    return [e async for e in db["events"].find({}, {"_id": 0}).sort("time", 1)]


# --- probes file -----------------------------------------------------------------------------------------------

def parse(text, tmp_path):
    path = tmp_path / "probes.toml"
    path.write_text(text)
    return monitor.load_probes(str(path))


def test_example_files_are_valid(tmp_path):
    assert monitor.load_probes(str(ROOT / "deploy" / "probes.toml")) == []
    probes = monitor.load_probes(str(ROOT / "deploy" / "probes.example.toml"))
    assert [p.name for p in probes] == ["rhinestone-health", "doc-onlook-port"]
    assert probes[0].target.endswith("/healthz") and probes[1].target == "host.docker.internal:2112"


def test_probe_defaults(tmp_path):
    probe = parse('[[probe]]\nname="a"\ntype="http"\nurl="http://x.example/h"\n', tmp_path)[0]
    assert (probe.interval, probe.timeout, probe.fail_after, probe.recover_after, probe.expect_status) == (
        10, 5, 2, 1, 0)


def test_no_file_means_no_probes():
    assert monitor.load_probes("") == []


@pytest.mark.parametrize("text,message", [
    ('[[probe]]\ntype="tcp"\nhost="h"\nport=1\n', "not valid"),
    ('[[probe]]\nname="bad name"\ntype="tcp"\nhost="h"\nport=1\n', "not valid"),
    ('[[probe]]\nname="a"\ntype="ping"\n', "type must be"),
    ('[[probe]]\nname="a"\ntype="http"\nurl="ftp://x/y"\n', "http:// or https://"),
    ('[[probe]]\nname="a"\ntype="http"\nurl="file:///etc/passwd"\n', "http:// or https://"),
    ('[[probe]]\nname="a"\ntype="http"\n', "http:// or https://"),
    ('[[probe]]\nname="a"\ntype="tcp"\nport=80\n', "host is required"),
    ('[[probe]]\nname="a"\ntype="tcp"\nhost="h"\n', "port is required"),
    ('[[probe]]\nname="a"\ntype="tcp"\nhost="h"\nport=70000\n', "port must be between"),
    ('[[probe]]\nname="a"\ntype="tcp"\nhost="h"\nport=1\ninterval=0\n', "interval must be between"),
    ('[[probe]]\nname="a"\ntype="tcp"\nhost="h"\nport=1\ntimeout="x"\n', "must be a number"),
    ('[[probe]]\nname="a"\ntype="tcp"\nhost="h"\nport=1\nmystery=1\n', "unknown keys: mystery"),
    ('[[probe]]\nname="a"\ntype="tcp"\nhost="h"\nport=1\n[[probe]]\nname="a"\ntype="tcp"\nhost="h"\nport=2\n',
     "duplicate probe name"),
    ('colour = "blue"\n', "unknown top-level keys"),
    ("not = = toml", "invalid probes file"),
])
def test_invalid_probe_files_are_refused(tmp_path, text, message):
    with pytest.raises(monitor.ProbeConfigError, match=message):
        parse(text, tmp_path)


def test_missing_probe_file_is_refused():
    with pytest.raises(monitor.ProbeConfigError, match="cannot read the probes file"):
        monitor.load_probes("/nonexistent/probes.toml")


def test_monitor_refuses_to_start_with_a_bad_probe_file(monkeypatch, tmp_path, capsys):
    bad = tmp_path / "p.toml"
    bad.write_text('[[probe]]\nname="a"\ntype="ping"\n')
    monkeypatch.setattr(config, "PROBES_FILE", str(bad))
    assert monitor.main() == 1
    assert "Refusing to start" in capsys.readouterr().err


def test_bad_webhook_url_is_refused():
    for url in ("ftp://x/y", "not a url", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            monitor.Notifier(url)


# --- HTTP and TCP checks -----------------------------------------------------------------------------------------

class OkHandler(RequestHandler):
    def get(self):
        self.write("service ok")


class FailHandler(RequestHandler):
    def get(self):
        self.set_status(503)
        self.write("down for maintenance")


class SlowHandler(RequestHandler):
    async def get(self):
        await asyncio.sleep(3)
        self.write("late")


class RedirectHandler(RequestHandler):
    def get(self):
        self.redirect("/ok")


@pytest_asyncio.fixture
async def target():
    sock, port = bind_unused_port()
    server = HTTPServer(Application([(r"/ok", OkHandler), (r"/fail", FailHandler), (r"/slow", SlowHandler),
                                     (r"/redirect", RedirectHandler)]))
    server.add_sockets([sock])
    yield port
    server.stop()


def http(port, path, **kwargs):
    return monitor.Probe(name="p", type="http", url="http://127.0.0.1:%d%s" % (port, path), timeout=1, **kwargs)


async def test_http_ok(target):
    result = await monitor.check_http(http(target, "/ok"))
    assert result.ok and result.detail.startswith("HTTP 200") and result.latency_ms is not None


async def test_http_error_status_is_down(target):
    result = await monitor.check_http(http(target, "/fail"))
    assert not result.ok and "HTTP 503" in result.detail
    assert "maintenance" not in result.detail  # response bodies are never copied into state or alerts


async def test_http_expected_status_and_text(target):
    assert (await monitor.check_http(http(target, "/fail", expect_status=503))).ok
    assert (await monitor.check_http(http(target, "/ok", expect_text="service"))).ok
    missing = await monitor.check_http(http(target, "/ok", expect_text="database"))
    assert not missing.ok and "expected text is missing" in missing.detail


async def test_http_redirects_are_not_followed(target):
    result = await monitor.check_http(http(target, "/redirect"))
    assert not result.ok and "HTTP 302" in result.detail


async def test_http_timeout(target):
    result = await monitor.check_http(http(target, "/slow"))
    assert not result.ok and "timeout" in result.detail


async def test_http_connection_refused():
    sock, port = bind_unused_port()
    sock.close()
    result = await monitor.check_http(http(port, "/ok"))
    assert not result.ok and result.detail == "connection refused"


async def test_http_unresolvable_host():
    probe = monitor.Probe(name="p", type="http", url="http://no-such-host.invalid/", timeout=2)
    result = await monitor.check_http(probe)
    assert not result.ok and result.detail


async def test_tcp_open_and_closed_ports():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    probe = monitor.Probe(name="p", type="tcp", host="127.0.0.1", port=port, timeout=1)
    assert (await monitor.check_tcp(probe)).ok
    server.close()
    await server.wait_closed()
    result = await monitor.check_tcp(probe)
    assert not result.ok and result.detail == "connection refused"


# --- state machine ----------------------------------------------------------------------------------------------

async def test_fresh_agent_is_up_without_an_alert(db):
    clock = Clock()
    await add_machine(db, last_online=clock.now - 2)
    mon, capture = make_monitor(clock)
    await mon.cycle()
    await mon.notifier.drain()
    assert (await state_of(db, "agent:box"))["state"] == "up"
    assert capture.bodies == [] and await events(db) == []


async def test_silent_agent_goes_down_then_recovers_with_alerts(db, caplog):
    clock = Clock()
    await add_machine(db, last_online=clock.now)
    mon, capture = make_monitor(clock)
    await mon.cycle()
    clock.now += 30
    with caplog.at_level(logging.INFO, logger="observer.alerts"):
        await mon.cycle()
        await mon.notifier.drain()
        assert (await state_of(db, "agent:box"))["state"] == "down"
        assert [b["event"] for b in capture.bodies] == ["down"]
        assert capture.bodies[0]["name"] == "box" and "no data for 30s" in capture.bodies[0]["detail"]
        assert capture.bodies[0]["text"].startswith("[DOWN] agent box")
        await mon.cycle()  # still down: no repeat alert
        await db["machines"].update_one({"name": "box"}, {"$set": {"last_online": str(int(clock.now))}})
        await mon.cycle()
        await mon.notifier.drain()
    assert [b["event"] for b in capture.bodies] == ["down", "up"]
    assert [e["event"] for e in await events(db)] == ["down", "up"]
    assert [r.levelno for r in caplog.records if r.name == "observer.alerts"] == [logging.WARNING, logging.INFO]


async def test_threshold_follows_the_agents_own_interval(db):
    clock = Clock()
    await add_machine(db, last_online=clock.now, interval=60)
    mon, _ = make_monitor(clock)
    await mon.cycle()
    clock.now += 100
    await mon.cycle()
    assert (await state_of(db, "agent:box"))["state"] == "up"      # 100 s < 3 x 60 s
    clock.now += 100
    await mon.cycle()
    assert (await state_of(db, "agent:box"))["state"] == "down"


async def test_first_observation_of_a_silent_agent_is_silent(db):
    clock = Clock()
    await add_machine(db, last_online=clock.now - 3600)
    mon, capture = make_monitor(clock)
    await mon.cycle()
    await mon.notifier.drain()
    assert (await state_of(db, "agent:box"))["state"] == "down" and capture.bodies == []


async def test_restart_does_not_repeat_alerts_but_reports_changes_made_while_down(db):
    clock = Clock()
    await add_machine(db, last_online=clock.now)
    first, capture = make_monitor(clock)
    await first.cycle()
    clock.now += 30
    await first.cycle()
    await first.notifier.drain()
    assert len(capture.bodies) == 1
    second, capture2 = make_monitor(clock)            # a new process picks the state up from the database
    await second.cycle()
    await second.notifier.drain()
    assert capture2.bodies == []
    await db["machines"].update_one({"name": "box"}, {"$set": {"last_online": str(int(clock.now))}})
    await second.cycle()
    await second.notifier.drain()
    assert [b["event"] for b in capture2.bodies] == ["up"]


async def test_deleted_machine_and_removed_probe_lose_their_state(db):
    clock = Clock()
    await add_machine(db, last_online=clock.now)
    probe = monitor.Probe(name="svc", type="tcp", host="127.0.0.1", port=1)

    async def ok(_):
        return monitor.Observation(True, "fine")

    mon, _ = make_monitor(clock, [probe], runner=ok)
    await mon.cycle()
    assert {d["_id"] async for d in db["states"].find()} == {"agent:box", "probe:svc"}
    await db["machines"].delete_many({})
    mon.probes = []
    await mon.cycle()
    assert await db["states"].count_documents({}) == 0


async def test_machine_with_garbage_last_online_is_down_not_a_crash(db):
    clock = Clock()
    await db["machines"].insert_one({"name": "odd", "last_online": "not-a-number", "ip": "x"})
    mon, _ = make_monitor(clock)
    await mon.cycle()
    assert (await state_of(db, "agent:odd"))["state"] == "down"


# --- probes through the state machine ---------------------------------------------------------------------------

def scripted(*results):
    """A probe runner that returns the given outcomes in order (True = up)."""
    outcomes = iter(results)

    async def runner(_):
        ok = next(outcomes)
        return monitor.Observation(ok, "HTTP 200" if ok else "HTTP 503")

    return runner


async def test_probe_needs_consecutive_failures_before_it_is_down(db):
    clock = Clock()
    probe = monitor.Probe(name="svc", type="http", url="http://x.example/", interval=10, fail_after=2)
    mon, capture = make_monitor(clock, [probe], runner=scripted(True, False, True, False, False, True))
    states = []
    for _ in range(6):
        await mon.cycle()
        states.append((await state_of(db, "probe:svc"))["state"])
        clock.now += 10
    await mon.notifier.drain()
    assert states == ["up", "up", "up", "up", "down", "up"]
    assert [b["event"] for b in capture.bodies] == ["down", "up"]


async def test_probe_that_fails_from_the_start_alerts_once_confirmed(db):
    clock = Clock()
    probe = monitor.Probe(name="svc", type="tcp", host="h", port=1, interval=10, fail_after=2)
    mon, capture = make_monitor(clock, [probe], runner=scripted(False, False, False))
    for _ in range(3):
        await mon.cycle()
        clock.now += 10
    await mon.notifier.drain()
    assert (await state_of(db, "probe:svc"))["state"] == "down"
    assert [b["event"] for b in capture.bodies] == ["down"] and capture.bodies[0]["kind"] == "probe"


async def test_probes_run_on_their_own_schedule(db):
    clock = Clock()
    calls = []

    async def runner(probe):
        calls.append((probe.name, clock.now))
        return monitor.Observation(True, "ok")

    fast = monitor.Probe(name="fast", type="tcp", host="h", port=1, interval=5)
    slow = monitor.Probe(name="slow", type="tcp", host="h", port=1, interval=20)
    mon, _ = make_monitor(clock, [fast, slow], runner=runner)
    for _ in range(5):
        await mon.cycle()
        clock.now += 5
    assert [c for c in calls if c[0] == "fast"].__len__() == 5 and [c for c in calls if c[0] == "slow"].__len__() == 2


async def test_a_crashing_or_hanging_runner_counts_as_a_failed_check(db):
    clock = Clock()

    async def boom(_):
        raise RuntimeError("secret internals")

    probe = monitor.Probe(name="svc", type="tcp", host="h", port=1, fail_after=1)
    mon, capture = make_monitor(clock, [probe], runner=boom)
    await mon.cycle()
    state = await state_of(db, "probe:svc")
    assert state["state"] == "down" and state["detail"] == "RuntimeError"


# --- leadership --------------------------------------------------------------------------------------------------

async def test_only_one_monitor_holds_the_lease(db):
    clock = Clock()
    first, _ = make_monitor(clock, holder="one")
    second, _ = make_monitor(clock, holder="two")
    assert await first.acquire_lease() is True
    assert await second.acquire_lease() is False
    assert await first.acquire_lease() is True            # renewing is fine
    clock.now += monitor.LEASE_SECONDS + 1
    assert await second.acquire_lease() is True            # takeover after expiry
    assert await first.acquire_lease() is False


async def test_idle_monitor_does_not_alert(db, monkeypatch):
    monkeypatch.setattr(config, "MONITOR_TICK_SECONDS", 0.05)
    clock = Clock()
    await add_machine(db, last_online=clock.now)
    leader, cap1 = make_monitor(clock, holder="one")
    idle, cap2 = make_monitor(clock, holder="two")
    tasks = [asyncio.create_task(leader.run()), asyncio.create_task(idle.run())]
    for _ in range(100):                                      # wait for the first observation before time moves
        if await state_of(db, "agent:box"):
            break
        await asyncio.sleep(0.05)
    clock.now += 60
    await asyncio.sleep(0.4)
    for m in (leader, idle):
        m.stop_event.set()
    await asyncio.gather(*tasks)
    assert len(cap1.bodies) + len(cap2.bodies) == 1


# --- notifier ----------------------------------------------------------------------------------------------------

EVENT = {"event": "down", "kind": "agent", "name": "box", "state": "down", "previous": "up", "since": 1.0,
         "detail": "no data for 30s", "time": 2.0}


async def test_webhook_payload_and_signature():
    capture = Capture()
    notifier = monitor.Notifier("http://hook.invalid/x", "s3cret-value", post=capture)
    notifier.notify(EVENT)
    await notifier.drain()
    headers, body = capture.headers[0]
    expected = "sha256=" + hmac.new(b"s3cret-value", body, hashlib.sha256).hexdigest()
    assert headers["X-Observer-Signature"] == expected and headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["text"] == "[DOWN] agent box: no data for 30s"
    assert {"event", "kind", "name", "state", "previous", "since", "detail", "time"} <= set(payload)


async def test_no_signature_header_without_a_secret():
    capture = Capture()
    notifier = monitor.Notifier("http://hook.invalid/x", "", post=capture)
    notifier.notify(EVENT)
    await notifier.drain()
    assert "X-Observer-Signature" not in capture.headers[0][0]


async def test_webhook_is_retried_then_succeeds(caplog):
    capture = Capture(fail_first=2)
    notifier = monitor.Notifier("http://hook.invalid/x", retry_delays=(0.01,), post=capture)
    with caplog.at_level(logging.WARNING, logger="observer.monitor"):
        notifier.notify(EVENT)
        await notifier.drain()
    assert capture.calls == 3 and len(capture.bodies) == 1
    assert "attempt 1/3" in caplog.text and "hook.invalid" in caplog.text


async def test_webhook_gives_up_after_three_attempts_and_still_logs(caplog):
    capture = Capture(fail_first=99)
    notifier = monitor.Notifier("http://hook.invalid/x?token=SECRETTOKEN", retry_delays=(0.01,), post=capture)
    with caplog.at_level(logging.INFO):
        notifier.notify(EVENT)
        await notifier.drain()
    assert capture.calls == 3
    assert "gave up" in caplog.text and "[DOWN] agent box" in caplog.text
    assert "SECRETTOKEN" not in caplog.text                    # only the host is ever logged


async def test_without_a_webhook_events_are_only_logged(caplog):
    notifier = monitor.Notifier()
    with caplog.at_level(logging.INFO, logger="observer.alerts"):
        notifier.notify(dict(EVENT, event="up", state="up", detail="back"))
    assert "[UP] agent box: back" in caplog.text


class Hook(RequestHandler):
    received = []

    def post(self):
        Hook.received.append((dict(self.request.headers), self.request.body))
        self.set_status(Hook.status)


async def test_webhook_over_real_http():
    Hook.received, Hook.status = [], 200
    sock, port = bind_unused_port()
    server = HTTPServer(Application([(r"/hook", Hook)]))
    server.add_sockets([sock])
    notifier = monitor.Notifier("http://127.0.0.1:%d/hook" % port, "k")
    notifier.notify(EVENT)
    await notifier.drain()
    server.stop()
    headers, body = Hook.received[0]
    assert json.loads(body)["name"] == "box" and headers["X-Observer-Signature"].startswith("sha256=")


async def test_webhook_http_error_status_counts_as_failure():
    Hook.received, Hook.status = [], 500
    sock, port = bind_unused_port()
    server = HTTPServer(Application([(r"/hook", Hook)]))
    server.add_sockets([sock])
    notifier = monitor.Notifier("http://127.0.0.1:%d/hook" % port, retry_delays=(0.01,))
    notifier.notify(EVENT)
    await notifier.drain()
    server.stop()
    assert len(Hook.received) == 3


# --- dashboard API and pages -------------------------------------------------------------------------------------

async def test_state_and_event_endpoints_require_a_session(anon_dash):
    for action in ("GET_STATES", "GET_EVENTS"):
        assert (await anon_dash.post("/api", data={"action": action})).status_code == 401


async def test_dashboard_reports_server_side_state(dash, db):
    clock = Clock(1_000_000.0)
    await add_machine(db, last_online=clock.now)
    mon, _ = make_monitor(clock)
    await mon.acquire_lease()
    await mon.cycle()
    clock.now += 60
    await mon.cycle()
    machines = json.loads((await dash.post("/api", data={"action": "GET_REMOTE_MACHINES"})).json()["remoteMachines"])
    assert machines["machines"][0]["state"] == "down" and machines["machines"][0]["state_since"] == clock.now
    body = (await dash.post("/api", data={"action": "GET_STATES"})).json()
    assert [(s["kind"], s["name"], s["state"]) for s in body["states"]] == [("agent", "box", "down")]
    assert body["monitor"]["heartbeat"] is not None
    assert (await dash.post("/api", data={"action": "GET_EVENTS"})).json()["events"][0]["event"] == "down"


async def test_dashboard_says_when_no_monitor_is_running(dash, db):
    body = (await dash.post("/api", data={"action": "GET_STATES"})).json()
    assert body["monitor"]["running"] is False and body["states"] == []


async def test_deleting_a_machine_removes_its_state(dash, db):
    clock = Clock()
    await add_machine(db, last_online=clock.now)
    mon, _ = make_monitor(clock)
    await mon.cycle()
    await dash.post("/api", data={"action": "DELETE_REMOTE_MACHINE", "machine_id": "box"})
    assert await state_of(db, "agent:box") is None


async def test_status_page_needs_a_login_and_renders(dash, anon_dash):
    assert (await anon_dash.get("/status")).status_code == 302
    page = (await dash.get("/status")).text
    assert "Recent events" in page and "status.js" in page


def test_browser_no_longer_decides_online_or_offline():
    script = (ROOT / "static" / "js" / "shared-scripts.js").read_text()
    assert "timeDiff" not in script and "lastOnlineStamp" not in script and "remoteMachines[i].state" in script


def test_status_page_script_builds_rows_with_text_only():
    script = (ROOT / "static" / "js" / "status.js").read_text()
    assert ".html(" not in script and "innerHTML" not in script


# --- the whole chain: real listener, real agent, monitor, webhook -----------------------------------------------

async def test_agent_outage_and_recovery_are_detected_and_alerted(db, monkeypatch):
    monkeypatch.setattr(config, "MONITOR_TICK_SECONDS", 0.1)
    token = await auth.create_agent("box")
    sock, port = bind_unused_port()
    listener = root.TCPListener()
    listener.add_sockets([sock])
    env = {"OBSERVER_AGENT_SERVER": "127.0.0.1", "OBSERVER_AGENT_PORT": str(port), "OBSERVER_AGENT_NAME": "box",
           "OBSERVER_AGENT_TOKEN": token, "OBSERVER_AGENT_INTERVAL": "0.1"}
    capture = Capture()
    notifier = monitor.Notifier("http://hook.invalid/x", post=capture)
    mon = monitor.Monitor([], notifier, down_after=1.0)
    monitor_task = asyncio.create_task(mon.run())

    async def wait_for_event(kind, timeout=15):
        for _ in range(int(timeout / 0.1)):
            if [b for b in capture.bodies if b["event"] == kind]:
                return True
            await asyncio.sleep(0.1)
        return False

    first = agent.Agent(agent.load_config(env))
    task = asyncio.create_task(first.run())
    for _ in range(100):
        state = await state_of(db, "agent:box")
        if state and state["state"] == "up":
            break
        await asyncio.sleep(0.1)
    assert state and state["state"] == "up" and capture.bodies == []

    first.stop()                                              # the agent stops sending
    await asyncio.wait_for(task, 5)
    assert await wait_for_event("down")

    second = agent.Agent(agent.load_config(env))              # and comes back
    task = asyncio.create_task(second.run())
    assert await wait_for_event("up")
    second.stop()
    await asyncio.wait_for(task, 5)
    mon.stop_event.set()
    await asyncio.wait_for(monitor_task, 5)
    listener.stop()
    assert [b["event"] for b in capture.bodies] == ["down", "up"]


# --- misc --------------------------------------------------------------------------------------------------------

def test_agent_sends_its_interval_and_the_server_accepts_it():
    import validation
    specs = agent.get_specs("box", 2.5)
    assert specs["interval"] == 2.5 and validation.clean_record(specs)["interval"] == 2.5
    assert "interval" not in agent.get_specs("box")


@pytest.mark.parametrize("interval", [0, -1, 100000, "fast", True])
def test_server_rejects_a_bad_interval(interval):
    import validation
    with pytest.raises(ValueError):
        validation.clean_record(dict(agent.get_specs("box"), interval=interval))


async def test_events_have_a_ttl_index(db, monkeypatch):
    monkeypatch.setattr(config, "EVENT_RETENTION_DAYS", 7)
    await db_module.ensure_indexes()
    indexes = {s["name"]: s async for s in await db["events"].list_indexes()}
    assert indexes["ts_1"]["expireAfterSeconds"] == 7 * 86400


def test_make_stat_is_still_valid_for_the_listener():
    assert make_stat()["name"] == "box"
