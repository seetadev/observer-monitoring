"""Fixed, parameterised page-load tests (Playwright). The only inputs are a URL, a concurrency and a duration.

There is no way to supply code or steps: every virtual user repeatedly loads the URL in its own isolated browser
context (fresh load, HTTP cache off) and records the browser's own navigation timings.

Safety: the target host must be on OBSERVER_LOADTEST_ALLOWED_HOSTS; every request the browser makes (including
redirects and sub-resources) is checked against the same list and blocked otherwise; addresses in the link-local
range (cloud metadata) are never allowed; concurrency and duration are capped; one test runs at a time.
"""
import asyncio
import dataclasses
import fnmatch
import ipaddress
import logging
import socket
import statistics
import sys
import time
import uuid
from urllib.parse import urlsplit

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import config
import db as db_module

log = logging.getLogger("observer.loadtest")

PARAM_KEYS = {"url", "concurrency", "duration"}
METRICS = ("wall_ms", "ttfb_ms", "dom_content_loaded_ms", "load_ms", "transfer_bytes", "resources", "resource_bytes")
MAX_SAMPLES = 2000
LEASE_SECONDS = 10
MAX_QUEUE = 5
BLOCKED_NAMES = {"metadata", "metadata.google.internal", "instance-data"}
LINK_LOCAL = [ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10")]

# The one script the browser runs on each page: reads the timings it already recorded. Nothing user-controlled.
TIMING_JS = """() => {
  const n = performance.getEntriesByType('navigation')[0] || {};
  const res = performance.getEntriesByType('resource');
  return {ttfb: n.responseStart || 0, dcl: n.domContentLoadedEventEnd || 0, load: n.loadEventEnd || 0,
          transfer: n.transferSize || 0, resources: res.length,
          resource_bytes: res.reduce((a, r) => a + (r.transferSize || 0), 0)};
}"""


class LoadtestError(ValueError):
    """Invalid parameters or a target that is not allowed."""


@dataclasses.dataclass(frozen=True)
class Params:
    url: str
    concurrency: int
    duration: int

    def as_dict(self):
        return dataclasses.asdict(self)


# --- validation --------------------------------------------------------------------------------------------------

def _ip(text):
    try:
        return ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return None


def always_blocked(hostname):
    host = hostname.lower().rstrip(".")
    if host in BLOCKED_NAMES:
        return True
    ip = _ip(host)
    return bool(ip) and any(ip in net for net in LINK_LOCAL)


def host_allowed(hostname, allowed=None):
    """Exact name, "*.suffix" or IP/CIDR entries; link-local and metadata names are never allowed."""
    allowed = config.LOADTEST_ALLOWED_HOSTS if allowed is None else allowed
    host = (hostname or "").lower().rstrip(".")
    if not host or always_blocked(host):
        return False
    ip = _ip(host)
    for entry in (e.strip().lower() for e in allowed):
        if entry.startswith("*."):
            if not ip and fnmatch.fnmatch(host, entry):
                return True
            continue
        if ip:
            try:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                pass
        elif host == entry:
            return True
    return False


def check_target(url, allowed=None):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LoadtestError("the URL must start with http:// or https:// and name a host")
    if parts.username or parts.password:
        raise LoadtestError("the URL must not contain credentials")
    if len(url) > 2000:
        raise LoadtestError("the URL is too long")
    try:
        parts.port
    except ValueError:
        raise LoadtestError("the URL has an invalid port") from None
    if always_blocked(parts.hostname):
        raise LoadtestError("this address is never allowed")
    if not host_allowed(parts.hostname, allowed):
        raise LoadtestError("%s is not in OBSERVER_LOADTEST_ALLOWED_HOSTS" % parts.hostname)


def parse_params(raw, allowed=None):
    """Strict schema: exactly url, concurrency, duration. Anything else (a script, steps, ...) is refused."""
    if not isinstance(raw, dict):
        raise LoadtestError("parameters must be an object")
    extra = sorted(set(raw) - PARAM_KEYS)
    if extra:
        raise LoadtestError("unknown parameter(s): " + ", ".join(extra))
    missing = sorted(PARAM_KEYS - set(raw))
    if missing:
        raise LoadtestError("missing parameter(s): " + ", ".join(missing))
    url = raw["url"]
    if not isinstance(url, str):
        raise LoadtestError("url must be text")
    try:
        if isinstance(raw["concurrency"], bool) or isinstance(raw["duration"], bool):
            raise ValueError
        concurrency, duration = int(raw["concurrency"]), int(raw["duration"])
    except (TypeError, ValueError):
        raise LoadtestError("concurrency and duration must be whole numbers") from None
    if not 1 <= concurrency <= config.LOADTEST_MAX_CONCURRENCY:
        raise LoadtestError("concurrency must be between 1 and %d" % config.LOADTEST_MAX_CONCURRENCY)
    if not 5 <= duration <= config.LOADTEST_MAX_DURATION:
        raise LoadtestError("duration must be between 5 and %d seconds" % config.LOADTEST_MAX_DURATION)
    check_target(url.strip(), allowed)
    return Params(url.strip(), concurrency, duration)


def resolves_to_blocked(hostname):
    """True if the name resolves to a link-local (metadata) address. Checked once, before the run starts."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False        # the page load will simply fail
    return any(always_blocked(info[4][0]) for info in infos)


# --- statistics --------------------------------------------------------------------------------------------------

def percentile(values, pct):
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, -(-len(ordered) * pct // 100))      # nearest rank
    return ordered[int(rank) - 1]


def summarise(values):
    if not values:
        return None
    return {"count": len(values), "min": min(values), "mean": round(statistics.fmean(values), 2),
            "p50": percentile(values, 50), "p95": percentile(values, 95), "max": max(values)}


def build_result(samples, errors, blocked, blocked_hosts, elapsed):
    ok = [s for s in samples if s["ok"]]
    codes = {}
    for sample in samples:
        key = str(sample["status"]) if sample["status"] else "none"
        codes[key] = codes.get(key, 0) + 1
    return {"loads": len(samples), "ok_loads": len(ok), "errors": errors, "status_codes": codes,
            "throughput_per_s": round(len(samples) / elapsed, 2) if elapsed > 0 else 0,
            "elapsed_s": round(elapsed, 2), "blocked_requests": blocked,
            "blocked_hosts": sorted(blocked_hosts)[:10],
            "summary": {metric: summarise([s[metric] for s in ok if s.get(metric) is not None]) for metric in METRICS}}


# --- the browser run -----------------------------------------------------------------------------------------------

def _error_kind(exc):
    text = (exc.__class__.__name__ + " " + str(exc)).lower()
    if "timeout" in text:
        return "timeout"
    if "net::" in text or "refused" in text or "dns" in text or "name_not_resolved" in text:
        return "network"
    return "other"


async def run_loadtest(params, allowed=None, stop=None, progress=None, no_sandbox=None):
    """Run the scenario and return (result, samples). `stop` is an asyncio.Event that ends the run early."""
    from playwright.async_api import async_playwright
    allowed = config.LOADTEST_ALLOWED_HOSTS if allowed is None else allowed
    stop = stop or asyncio.Event()
    no_sandbox = config.LOADTEST_NO_SANDBOX if no_sandbox is None else no_sandbox
    host = urlsplit(params.url).hostname
    if resolves_to_blocked(host):
        raise LoadtestError("the host resolves to a blocked address")
    samples, errors, blocked_hosts = [], {"timeout": 0, "network": 0, "http_error": 0, "other": 0}, set()
    state = {"blocked": 0, "loads": 0, "n": 0}
    timeout_ms = config.LOADTEST_PAGE_TIMEOUT * 1000

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"] if no_sandbox else [])
        started = time.monotonic()
        deadline = started + params.duration

        async def decide(cdp, event):
            """Allow or fail one request. CDP's Fetch domain pauses every request, including each redirect hop
            (Playwright's own route handler only sees the first hop)."""
            url = event["request"]["url"]
            request_host = urlsplit(url).hostname
            scheme = urlsplit(url).scheme
            try:
                if scheme in ("data", "blob", "about") or (request_host and host_allowed(request_host, allowed)):
                    await cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
                    return
                state["blocked"] += 1
                if request_host and len(blocked_hosts) < 50:
                    blocked_hosts.add(request_host)
                await cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})
            except Exception:  # noqa: BLE001  (the page may have navigated away meanwhile)
                pass

        async def user(index):
            context = await browser.new_context(service_workers="block", accept_downloads=False)
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)
            pending = set()

            def on_paused(event):
                task = asyncio.ensure_future(decide(cdp, event))
                pending.add(task)
                task.add_done_callback(pending.discard)

            cdp.on("Fetch.requestPaused", on_paused)
            await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
            await cdp.send("Network.enable")
            await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})   # every load is a fresh load
            while time.monotonic() < deadline and not stop.is_set():
                begun = time.monotonic()
                sample = {"t": round(begun - started, 2), "user": index, "status": 0, "ok": False}
                try:
                    response = await page.goto(params.url, wait_until="load", timeout=timeout_ms)
                    sample["wall_ms"] = round((time.monotonic() - begun) * 1000, 1)
                    sample["status"] = response.status if response else 0
                    timing = await page.evaluate(TIMING_JS)
                    sample.update(ttfb_ms=round(timing["ttfb"], 1), dom_content_loaded_ms=round(timing["dcl"], 1),
                                  load_ms=round(timing["load"], 1), transfer_bytes=int(timing["transfer"]),
                                  resources=int(timing["resources"]), resource_bytes=int(timing["resource_bytes"]))
                    if sample["status"] and sample["status"] < 400:
                        sample["ok"] = True
                    else:
                        errors["http_error"] += 1
                except Exception as exc:  # noqa: BLE001
                    errors[_error_kind(exc)] += 1
                    sample["wall_ms"] = round((time.monotonic() - begun) * 1000, 1)
                state["loads"] += 1
                if len(samples) < MAX_SAMPLES * 5:
                    samples.append(sample)
            await context.close()

        async def reporter():
            while time.monotonic() < deadline and not stop.is_set():
                await asyncio.sleep(1)
                if progress:
                    await progress({"elapsed": round(time.monotonic() - started, 1), "loads": state["loads"],
                                    "errors": sum(errors.values())})

        tasks = [asyncio.create_task(user(i)) for i in range(params.concurrency)]
        report = asyncio.create_task(reporter())
        try:
            await asyncio.gather(*tasks)
        finally:
            report.cancel()
            for task in tasks:
                task.cancel()
            await browser.close()
        elapsed = time.monotonic() - started
    result = build_result(samples, errors, state["blocked"], blocked_hosts, elapsed)
    result["cancelled"] = stop.is_set()
    return result, samples[:MAX_SAMPLES]


# --- the worker (database side) -----------------------------------------------------------------------------------

def now_utc():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)


async def enqueue(raw, allowed=None):
    """Validate and queue a run. Returns its id."""
    params = parse_params(raw, allowed)
    runs = db_module.get_db()["loadtests"]
    if await runs.count_documents({"status": {"$in": ["queued", "running"]}}) >= MAX_QUEUE:
        raise LoadtestError("too many tests are waiting; try again when one has finished")
    inserted = await runs.insert_one({"status": "queued", "params": params.as_dict(), "created": time.time(),
                                      "ts": now_utc(), "requested_by": "admin"})
    return str(inserted.inserted_id)


async def list_runs(limit=20):
    cursor = db_module.get_db()["loadtests"].find({}, {"samples": 0, "ts": 0}).sort("created", -1).limit(limit)
    return [_public(doc) async for doc in cursor]


async def get_run(run_id):
    try:
        oid = ObjectId(run_id)
    except Exception:  # noqa: BLE001
        return None
    doc = await db_module.get_db()["loadtests"].find_one({"_id": oid}, {"ts": 0})
    return _public(doc) if doc else None


def _public(doc):
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


async def request_cancel(run_id):
    try:
        oid = ObjectId(run_id)
    except Exception:  # noqa: BLE001
        return False
    result = await db_module.get_db()["loadtests"].update_one(
        {"_id": oid, "status": {"$in": ["queued", "running"]}}, {"$set": {"cancel_requested": True}})
    return result.matched_count == 1


async def worker_status():
    lease = await db_module.get_db()["locks"].find_one({"_id": "loadtest"})
    now = time.time()
    return {"running": bool(lease) and lease.get("expires_at", 0) >= now,
            "heartbeat": lease.get("heartbeat") if lease else None}


class Worker:
    def __init__(self, runner=run_loadtest, clock=time.time, holder=None):
        self.runner, self.clock = runner, clock
        self.holder = holder or uuid.uuid4().hex
        self.stop_event = asyncio.Event()

    async def acquire_lease(self):
        now = self.clock()
        try:
            await db_module.get_db()["locks"].update_one(
                {"_id": "loadtest", "$or": [{"holder": self.holder}, {"expires_at": {"$lt": now}}]},
                {"$set": {"holder": self.holder, "expires_at": now + LEASE_SECONDS, "heartbeat": now}}, upsert=True)
            return True
        except DuplicateKeyError:
            return False

    async def claim(self):
        return await db_module.get_db()["loadtests"].find_one_and_update(
            {"status": "queued"}, {"$set": {"status": "running", "started": self.clock(), "worker": self.holder}},
            sort=[("created", 1)], return_document=ReturnDocument.AFTER)

    async def fail_stale(self):
        """Runs left 'running' by a worker that died."""
        await db_module.get_db()["loadtests"].update_many(
            {"status": "running", "worker": {"$ne": self.holder}},
            {"$set": {"status": "failed", "finished": self.clock(), "error": "the worker stopped during the test"}})

    async def execute(self, doc):
        runs = db_module.get_db()["loadtests"]
        run_id = doc["_id"]
        if doc.get("cancel_requested"):
            await runs.update_one({"_id": run_id}, {"$set": {"status": "cancelled", "finished": self.clock()}})
            return
        try:
            params = parse_params(doc["params"])         # re-checked: the allow-list may have changed since queuing
        except LoadtestError as exc:
            await runs.update_one({"_id": run_id}, {"$set": {"status": "failed", "finished": self.clock(),
                                                             "error": str(exc)}})
            return
        stop = asyncio.Event()

        async def progress(info):
            now = self.clock()
            await self.acquire_lease()                    # keep the lease alive during a long run
            current = await runs.find_one_and_update({"_id": run_id}, {"$set": {"progress": info}},
                                                     projection={"cancel_requested": 1})
            if current and current.get("cancel_requested"):
                stop.set()
            return now

        try:
            result, samples = await self.runner(params, stop=stop, progress=progress)
        except LoadtestError as exc:
            await runs.update_one({"_id": run_id}, {"$set": {"status": "failed", "finished": self.clock(),
                                                             "error": str(exc)}})
            return
        except Exception:  # noqa: BLE001
            log.exception("load test %s failed", run_id)
            await runs.update_one({"_id": run_id}, {"$set": {"status": "failed", "finished": self.clock(),
                                                             "error": "the test could not be run"}})
            return
        status = "cancelled" if result.get("cancelled") else "done"
        await runs.update_one({"_id": run_id}, {"$set": {"status": status, "finished": self.clock(),
                                                         "result": result, "samples": samples}})

    async def run(self):
        await db_module.ensure_indexes()
        log.info("Load-test worker started")
        leader = False
        while not self.stop_event.is_set():
            try:
                acquired = await self.acquire_lease()
                if acquired and not leader:
                    await self.fail_stale()
                    log.info("Load-test worker is active")
                leader = acquired
                if leader:
                    doc = await self.claim()
                    if doc:
                        await self.execute(doc)
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Load-test worker cycle failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), 1.0)
            except asyncio.TimeoutError:
                pass


async def _amain(worker):
    import signal
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop_event.set)
    await worker.run()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.LOADTEST_ALLOWED_HOSTS:
        log.warning("OBSERVER_LOADTEST_ALLOWED_HOSTS is empty: every test will be refused until targets are listed")
    try:
        asyncio.run(_amain(Worker()))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
