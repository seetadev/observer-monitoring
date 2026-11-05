"""Up/down state, alerts and pull probes.

One `monitor` process (guarded by a database lease, so an accidental second copy stays idle) evaluates:

* agents: down when nothing has arrived for DOWN_AFTER_SECONDS (or three of the agent's own intervals);
* probes: HTTP or TCP checks listed in an operator-owned TOML file (never created through the web API).

States live in `states`, transitions in `events` (with a TTL). Every transition is logged and, if configured,
POSTed to a webhook (HMAC-signed when a secret is set).
"""
import asyncio
import dataclasses
import datetime
import hashlib
import hmac
import json
import logging
import socket
import sys
import time
import tomllib
import uuid
from urllib.parse import urlsplit

from pymongo.errors import DuplicateKeyError
from tornado.httpclient import AsyncHTTPClient

import auth
import config
import db as db_module

log = logging.getLogger("observer.monitor")
alert_log = logging.getLogger("observer.alerts")

LEASE_SECONDS = 10
MAX_BODY_BYTES = 64 * 1024
WEBHOOK_ATTEMPTS = 3
WEBHOOK_TIMEOUT = 5
PROBE_KEYS = {"name", "type", "url", "host", "port", "interval", "timeout", "expect_status", "expect_text",
              "fail_after", "recover_after"}


class ProbeConfigError(ValueError):
    """The probes file is invalid."""


# --- probes ---------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Probe:
    name: str
    type: str
    url: str = ""
    host: str = ""
    port: int = 0
    interval: float = 10.0
    timeout: float = 5.0
    expect_status: int = 0        # 0 = any 2xx
    expect_text: str = ""
    fail_after: int = 2
    recover_after: int = 1

    @property
    def target(self):
        return self.url if self.type == "http" else "%s:%d" % (self.host, self.port)


def _number(entry, key, default, low, high, integer=False):
    value = entry.get(key, default)
    try:
        value = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise ProbeConfigError("probe %r: %s must be a number" % (entry.get("name"), key)) from None
    if isinstance(entry.get(key), bool) or not low <= value <= high:
        raise ProbeConfigError("probe %r: %s must be between %s and %s" % (entry.get("name"), key, low, high))
    return value


def parse_probes(data):
    entries = data.get("probe", [])
    if not isinstance(entries, list):
        raise ProbeConfigError("[[probe]] entries expected")
    unknown_top = sorted(set(data) - {"probe"})
    if unknown_top:
        raise ProbeConfigError("unknown top-level keys: " + ", ".join(unknown_top))
    probes, seen = [], set()
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or not auth.NAME_PATTERN.match(name):
            raise ProbeConfigError("probe name %r is not valid (letters, digits, '.', '_', '-')" % (name,))
        if name in seen:
            raise ProbeConfigError("duplicate probe name %r" % name)
        seen.add(name)
        unknown = sorted(set(entry) - PROBE_KEYS)
        if unknown:
            raise ProbeConfigError("probe %r: unknown keys: %s" % (name, ", ".join(unknown)))
        kind = entry.get("type")
        if kind not in ("http", "tcp"):
            raise ProbeConfigError("probe %r: type must be 'http' or 'tcp'" % name)
        common = dict(name=name, type=kind, interval=_number(entry, "interval", 10, 1, 86400),
                      timeout=_number(entry, "timeout", 5, 0.5, 60),
                      fail_after=_number(entry, "fail_after", 2, 1, 100, integer=True),
                      recover_after=_number(entry, "recover_after", 1, 1, 100, integer=True))
        if kind == "http":
            url = entry.get("url")
            parts = urlsplit(url) if isinstance(url, str) else None
            if not parts or parts.scheme not in ("http", "https") or not parts.hostname:
                raise ProbeConfigError("probe %r: url must be an http:// or https:// URL" % name)
            probes.append(Probe(url=url, expect_status=_number(entry, "expect_status", 0, 0, 599, integer=True),
                                expect_text=str(entry.get("expect_text", "")), **common))
        else:
            host = entry.get("host")
            if not isinstance(host, str) or not host.strip():
                raise ProbeConfigError("probe %r: host is required" % name)
            if "port" not in entry:
                raise ProbeConfigError("probe %r: port is required" % name)
            probes.append(Probe(host=host.strip(), port=_number(entry, "port", None, 1, 65535, integer=True),
                                **common))
    return probes


def load_probes(path):
    if not path:
        return []
    try:
        with open(path, "rb") as handle:
            return parse_probes(tomllib.load(handle))
    except OSError as exc:
        raise ProbeConfigError("cannot read the probes file %s: %s" % (path, exc.strerror)) from None
    except tomllib.TOMLDecodeError as exc:
        raise ProbeConfigError("invalid probes file %s: %s" % (path, exc)) from None


@dataclasses.dataclass
class Observation:
    ok: bool
    detail: str
    latency_ms: float | None = None


def _describe(exc):
    if isinstance(exc, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "imeout" in exc.__class__.__name__ \
            or "imeout" in str(exc):
        return "timeout"
    return exc.__class__.__name__


async def check_tcp(probe):
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(probe.host, probe.port), probe.timeout)
    except asyncio.TimeoutError:
        return Observation(False, "timeout after %gs" % probe.timeout)
    except OSError as exc:
        return Observation(False, _describe(exc))
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    latency = (time.monotonic() - start) * 1000
    return Observation(True, "connected in %d ms" % latency, latency)


async def check_http(probe):
    client = AsyncHTTPClient(force_instance=True, max_body_size=MAX_BODY_BYTES)
    start = time.monotonic()
    try:
        response = await client.fetch(probe.url, method="GET", request_timeout=probe.timeout,
                                      follow_redirects=False, raise_error=False,
                                      headers={"User-Agent": "observer-monitor"})
    except Exception as exc:  # noqa: BLE001  (any failure to complete the request is a failed check)
        detail = _describe(exc)
        return Observation(False, "timeout after %gs" % probe.timeout if detail == "timeout" else detail)
    finally:
        client.close()
    latency = (time.monotonic() - start) * 1000
    if response.code == 599:  # tornado's code for "no HTTP response"
        error = response.error
        if error is None:
            return Observation(False, "no response")
        if "imeout" in str(error) or "imeout" in error.__class__.__name__:
            return Observation(False, "timeout after %gs" % probe.timeout)
        return Observation(False, _describe(error))
    wanted = probe.expect_status or None
    status_ok = response.code == wanted if wanted else 200 <= response.code < 300
    detail = "HTTP %d in %d ms" % (response.code, latency)
    if not status_ok:
        return Observation(False, "HTTP %d (expected %s)" % (response.code, wanted or "2xx"), latency)
    if probe.expect_text and probe.expect_text not in response.body.decode("utf-8", "replace"):
        return Observation(False, "HTTP %d but the expected text is missing" % response.code, latency)
    return Observation(True, detail, latency)


async def run_probe(probe):
    return await (check_http if probe.type == "http" else check_tcp)(probe)


# --- alerts ---------------------------------------------------------------------------------------------------

def event_text(event):
    prefix = "[DOWN]" if event["event"] == "down" else "[UP]"
    return "%s %s %s: %s" % (prefix, event["kind"], event["name"], event["detail"])


class Notifier:
    """Logs every transition and, if a URL is configured, POSTs it as JSON (signed when a secret is set)."""

    def __init__(self, url="", secret="", retry_delays=(1.0, 3.0), post=None):
        self.url, self.secret, self.retry_delays = url, secret, retry_delays
        self.post = post or self._post
        self._tasks = set()
        if url:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("OBSERVER_ALERT_WEBHOOK_URL must be an http:// or https:// URL")

    def notify(self, event):
        text = event_text(event)
        (alert_log.warning if event["event"] == "down" else alert_log.info)(text)
        if self.url:
            task = asyncio.ensure_future(self._deliver(dict(event, text=text)))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def drain(self):
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def sign(self, body):
        return "sha256=" + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()

    async def _post(self, body, headers):
        client = AsyncHTTPClient(force_instance=True)
        try:
            response = await client.fetch(self.url, method="POST", body=body, headers=headers,
                                          request_timeout=WEBHOOK_TIMEOUT, raise_error=False)
        finally:
            client.close()
        if response.code >= 300:
            raise OSError("webhook answered HTTP %d" % response.code)

    async def _deliver(self, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "observer-monitor"}
        if self.secret:
            headers["X-Observer-Signature"] = self.sign(body)
        host = urlsplit(self.url).hostname
        for attempt in range(WEBHOOK_ATTEMPTS):
            try:
                await self.post(body, headers)
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("Alert webhook to %s failed (attempt %d/%d): %s", host, attempt + 1, WEBHOOK_ATTEMPTS,
                            _describe(exc))
                if attempt < WEBHOOK_ATTEMPTS - 1:
                    await asyncio.sleep(self.retry_delays[min(attempt, len(self.retry_delays) - 1)])
        log.error("Alert webhook to %s gave up; the event is in the log and the events collection", host)
        return False


# --- state engine ---------------------------------------------------------------------------------------------

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


class Monitor:
    def __init__(self, probes=(), notifier=None, clock=time.time, runner=run_probe, down_after=None,
                 holder=None):
        self.probes = list(probes)
        self.notifier = notifier or Notifier()
        self.clock = clock
        self.runner = runner
        self.down_after = config.DOWN_AFTER_SECONDS if down_after is None else down_after
        self.holder = holder or uuid.uuid4().hex
        self.next_due = {}
        self.stop_event = asyncio.Event()

    # -- leadership: only one monitor acts at a time ------------------------------------------------------------
    async def acquire_lease(self):
        now = self.clock()
        try:
            await db_module.get_db()["locks"].update_one(
                {"_id": "monitor", "$or": [{"holder": self.holder}, {"expires_at": {"$lt": now}}]},
                {"$set": {"holder": self.holder, "expires_at": now + LEASE_SECONDS, "heartbeat": now}},
                upsert=True)
            return True
        except DuplicateKeyError:
            return False

    # -- state transitions --------------------------------------------------------------------------------------
    async def record(self, kind, name, observation, fail_after=1, recover_after=1):
        states = db_module.get_db()["states"]
        key = "%s:%s" % (kind, name)
        now = self.clock()
        previous = await states.find_one({"_id": key})
        failures = successes = 0
        state = "unknown"
        if previous:
            failures, successes, state = previous["failures"], previous["successes"], previous["state"]
        if observation.ok:
            successes, failures = successes + 1, 0
            if state != "up" and successes >= recover_after:
                state = "up"
        else:
            failures, successes = failures + 1, 0
            if state != "down" and failures >= fail_after:
                state = "down"
        since = previous["since"] if previous and previous["state"] == state else now
        document = {"kind": kind, "name": name, "state": state, "since": since, "checked_at": now,
                    "detail": observation.detail, "latency_ms": observation.latency_ms,
                    "failures": failures, "successes": successes}
        await states.replace_one({"_id": key}, document, upsert=True)
        if previous and previous["state"] != state and not (previous["state"] == "unknown" and state == "up"):
            await self._announce(kind, name, state, previous["state"], since, observation.detail)

    async def _announce(self, kind, name, state, previous, since, detail):
        event = {"event": state, "kind": kind, "name": name, "state": state, "previous": previous,
                 "since": since, "detail": detail, "time": self.clock(), "ts": now_utc()}
        await db_module.get_db()["events"].insert_one(dict(event))
        self.notifier.notify({k: v for k, v in event.items() if k != "ts"})

    async def check_agents(self):
        now = self.clock()
        names = set()
        async for machine in db_module.get_db()["machines"].find():
            name = machine["name"]
            names.add(name)
            try:
                last = float(machine.get("last_online"))
            except (TypeError, ValueError):
                last = 0.0
            limit = max(self.down_after, 3 * float(machine.get("interval") or 0))
            age = now - last
            ok = age <= limit
            detail = "last data %ds ago" % age if ok else "no data for %ds" % age
            await self.record("agent", name, Observation(ok, detail))
        await self._forget("agent", names)

    async def _forget(self, kind, keep):
        await db_module.get_db()["states"].delete_many({"kind": kind, "name": {"$nin": sorted(keep)}})

    async def check_probes(self):
        now = self.clock()
        due = [p for p in self.probes if self.next_due.get(p.name, 0) <= now]
        for probe in due:
            self.next_due[probe.name] = now + probe.interval

        async def one(probe):
            try:
                return probe, await asyncio.wait_for(self.runner(probe), probe.timeout + 2)
            except Exception as exc:  # noqa: BLE001
                return probe, Observation(False, _describe(exc))

        for probe, observation in await asyncio.gather(*(one(p) for p in due)):
            await self.record("probe", probe.name, observation, probe.fail_after, probe.recover_after)
        await self._forget("probe", {p.name for p in self.probes})

    async def cycle(self):
        await self.check_agents()
        await self.check_probes()

    async def run(self):
        await db_module.ensure_indexes()
        log.info("Monitor started (%d probes)", len(self.probes))
        leader = False
        while not self.stop_event.is_set():
            try:
                acquired = await self.acquire_lease()
                if acquired and not leader:
                    log.info("Monitor is active")
                elif leader and not acquired:
                    log.warning("Monitor lost the lease; idling")
                leader = acquired
                if leader:
                    await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Monitor cycle failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), config.MONITOR_TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
        await self.notifier.drain()


# --- reading state (used by the dashboard API) -----------------------------------------------------------------

async def read_states():
    database = db_module.get_db()
    states = [dict(doc, id=doc.pop("_id")) async for doc in database["states"].find().sort(
        [("kind", 1), ("name", 1)])]
    lease = await database["locks"].find_one({"_id": "monitor"})
    now = time.time()
    running = bool(lease) and lease.get("expires_at", 0) >= now
    return {"states": states, "monitor": {"running": running, "heartbeat": lease.get("heartbeat") if lease else None}}


async def read_events(limit=50):
    cursor = db_module.get_db()["events"].find({}, {"_id": 0, "ts": 0}).sort("time", -1).limit(limit)
    return await cursor.to_list(limit)


# --- entry point -------------------------------------------------------------------------------------------------

async def _amain(monitor):
    import signal
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, monitor.stop_event.set)
    await monitor.run()


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        probes = load_probes(config.PROBES_FILE)
        notifier = Notifier(config.ALERT_WEBHOOK_URL, config.ALERT_WEBHOOK_SECRET)
    except (ProbeConfigError, ValueError) as exc:
        print("Refusing to start: " + str(exc), file=sys.stderr)
        return 1
    try:
        asyncio.run(_amain(Monitor(probes, notifier)))
    except KeyboardInterrupt:
        pass
    return 0
