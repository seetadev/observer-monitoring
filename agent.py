"""Observer agent: collects host metrics with psutil and streams them to the Observer root server.

Standalone (needs only psutil, Python 3.11+). Non-interactive: configured from environment variables and/or a
TOML file. The token is never read from the file itself, only from OBSERVER_AGENT_TOKEN or a token file.

    OBSERVER_AGENT_SERVER=observer.example.org OBSERVER_AGENT_NAME=web-01 \\
    OBSERVER_AGENT_TOKEN_FILE=/etc/observer-agent/token python agent.py
"""
import argparse
import asyncio
import dataclasses
import datetime
import json
import logging
import os
import platform
import random
import re
import signal
import socket
import ssl
import stat
import sys
import time
import tomllib

import psutil

log = logging.getLogger("observer.agent")

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FILE_KEYS = {"server", "port", "name", "token_file", "nic", "interval", "tls", "tls_ca", "backoff_max", "log_level"}
CONNECT_TIMEOUT = 10
WRITE_TIMEOUT = 10
REJECT_WINDOW = 3.0       # a connection closed this soon after connecting was most likely refused
STABLE_AFTER = 30.0       # a connection that lasted this long resets the reconnect back-off


class AgentConfigError(ValueError):
    """The agent cannot start with this configuration."""


# --- configuration ---------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class AgentConfig:
    server: str
    port: int
    name: str
    token: str = dataclasses.field(repr=False)
    nic: str = "auto"
    interval: float = 1.0
    tls: bool = False
    tls_ca: str = ""
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    log_level: str = "INFO"


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _read_token_file(path):
    try:
        mode = os.stat(path).st_mode
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        raise AgentConfigError("cannot read the token file %s: %s" % (path, exc.strerror)) from None
    if mode & stat.S_IROTH:
        log.warning("The token file %s is readable by every user; restrict it (chmod 600).", path)
    return token


def load_config(env=None, config_path=None):
    """Build the configuration. Environment variables override the file."""
    env = os.environ if env is None else env
    config_path = config_path or env.get("OBSERVER_AGENT_CONFIG")
    values = {}
    if config_path:
        try:
            with open(config_path, "rb") as handle:
                values = tomllib.load(handle)
        except OSError as exc:
            raise AgentConfigError("cannot read the config file %s: %s" % (config_path, exc.strerror)) from None
        except tomllib.TOMLDecodeError as exc:
            raise AgentConfigError("invalid config file %s: %s" % (config_path, exc)) from None
        if "token" in values:
            raise AgentConfigError("the token must not be stored in the config file; use OBSERVER_AGENT_TOKEN "
                                   "or token_file")
        unknown = sorted(set(values) - FILE_KEYS)
        if unknown:
            raise AgentConfigError("unknown config keys: " + ", ".join(unknown))

    def pick(key, default=None):
        return env.get("OBSERVER_AGENT_" + key.upper(), values.get(key, default))

    token = env.get("OBSERVER_AGENT_TOKEN", "").strip()
    token_file = pick("token_file")
    if not token and token_file:
        token = _read_token_file(token_file)
    if not token:
        raise AgentConfigError("no token: set OBSERVER_AGENT_TOKEN or OBSERVER_AGENT_TOKEN_FILE")

    server = str(pick("server", "127.0.0.1")).strip()
    name = str(pick("name", socket.gethostname())).strip()
    if not NAME_PATTERN.match(name):
        raise AgentConfigError("OBSERVER_AGENT_NAME %r is not a valid name (letters, digits, '.', '_', '-'; "
                               "max 64; must start with a letter or digit)" % name)
    try:
        port = int(pick("port", 8889))
        interval = float(pick("interval", 1.0))
        backoff_max = float(pick("backoff_max", 60))
    except (TypeError, ValueError):
        raise AgentConfigError("port, interval and backoff_max must be numbers") from None
    if not 1 <= port <= 65535:
        raise AgentConfigError("port must be between 1 and 65535")
    if not 0.1 <= interval <= 3600:
        raise AgentConfigError("interval must be between 0.1 and 3600 seconds")
    if backoff_max < 1:
        raise AgentConfigError("backoff_max must be at least 1 second")
    tls_ca = str(pick("tls_ca", "") or "")
    tls = _truthy(pick("tls", False)) or bool(tls_ca)
    log_level = str(pick("log_level", "INFO")).upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise AgentConfigError("log_level must be DEBUG, INFO, WARNING or ERROR")
    return AgentConfig(server=server, port=port, name=name, token=token, nic=str(pick("nic", "auto")).strip(),
                       interval=interval, tls=tls, tls_ca=tls_ca, backoff_max=backoff_max, log_level=log_level)


# --- metrics ---------------------------------------------------------------------------------------------------

def is_loopback(nic):
    return nic == "lo" or nic.lower().startswith(("lo:", "loopback"))


def available_nics():
    return sorted(psutil.net_io_counters(pernic=True))


def validate_nic(nic):
    if nic == "auto":
        if not [n for n in available_nics() if not is_loopback(n)]:
            raise AgentConfigError("no network interface other than loopback found")
    elif nic not in available_nics():
        raise AgentConfigError("network interface %r not found; available: %s" % (nic, ", ".join(available_nics())))


def get_specs(name, interval=None):
    specs = {"machine": platform.platform(), "node": platform.node(),
             "architecture": [platform.architecture()[0], platform.architecture()[1]],
             "system": platform.system(), "release": platform.release(), "version": platform.version(),
             "name": name, "memory": psutil.virtual_memory().total}
    if interval is not None:
        specs["interval"] = interval  # lets the server judge "silent" relative to the sample interval
    return specs


class Collector:
    """Takes one sample per call. Rates are per second since the previous sample (0 for the first)."""

    def __init__(self, name, nic="auto", clock=time.monotonic):
        self.name, self.nic, self.clock = name, nic, clock
        self.previous = None
        self.disk_root = os.path.abspath(os.sep)
        psutil.cpu_percent(interval=None, percpu=True)  # prime: the first reading is meaningless

    def _network(self):
        counters = psutil.net_io_counters(pernic=True)
        chosen = [c for n, c in counters.items() if not is_loopback(n)] if self.nic == "auto" \
            else [counters[self.nic]]
        return {"bytes_sent": sum(c.bytes_sent for c in chosen), "bytes_recv": sum(c.bytes_recv for c in chosen),
                "packets_sent": [sum(c.packets_sent for c in chosen), sum(c.errout for c in chosen)],
                "packets_recv": [sum(c.packets_recv for c in chosen), sum(c.errin for c in chosen)]}

    def sample(self):
        now = self.clock()
        memory, swap, cpu_stats = psutil.virtual_memory(), psutil.swap_memory(), psutil.cpu_stats()
        disk, io = psutil.disk_usage(self.disk_root), psutil.disk_io_counters()
        stats = {
            "date": datetime.datetime.now().isoformat(), "name": self.name,
            "cpu": psutil.cpu_percent(interval=None, percpu=True), "cpu_count": psutil.cpu_count(),
            "cpu_ctx_switches": cpu_stats.ctx_switches, "cpu_interrupts": cpu_stats.interrupts,
            "ram": memory.percent, "ram-available": memory.available, "ram-used": memory.used,
            "swap": swap.percent, "swap-total": swap.total, "swap-used": swap.used,
            "disk_io_read": io.read_bytes if io else 0, "disk_io_write": io.write_bytes if io else 0,
            "disk_total": disk.total, "disk_used": disk.used,
            "uptime": max(0.0, time.time() - psutil.boot_time()),
        }
        stats.update(self._network())

        totals = {"dl_rate": stats["bytes_recv"], "ul_rate": stats["bytes_sent"],
                  "disk_read_rate": stats["disk_io_read"], "disk_write_rate": stats["disk_io_write"]}
        previous = self.previous
        for key, value in totals.items():
            if previous is None or now <= previous[0]:
                stats[key] = 0
            else:
                stats[key] = max(0, round((value - previous[1][key]) / (now - previous[0])))
        self.previous = (now, totals)
        return stats


# --- connection loop -------------------------------------------------------------------------------------------

def backoff_delays(base=1.0, cap=60.0, jitter=0.25, rng=random.random):
    """1, 2, 4 ... seconds up to `cap`, each spread by +/- `jitter` so many agents do not reconnect in step."""
    delay = base
    while True:
        yield min(cap, delay * (1 + jitter * (2 * rng() - 1)))
        delay = min(cap, delay * 2)


class Agent:
    def __init__(self, config, collector=None, rng=random.random):
        self.config = config
        self.collector = collector or Collector(config.name, config.nic)
        self.rng = rng
        self.stop_event = asyncio.Event()
        self.connections = 0

    def stop(self):
        self.stop_event.set()

    def _ssl(self):
        if not self.config.tls:
            return None
        return ssl.create_default_context(cafile=self.config.tls_ca or None)

    async def _sleep(self, seconds):
        """Sleep, but wake immediately when the agent is asked to stop."""
        try:
            await asyncio.wait_for(self.stop_event.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _session(self):
        """One connection. Returns normally when stopped; raises when the connection ends."""
        cfg = self.config
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(cfg.server, cfg.port, ssl=self._ssl(),
                                    server_hostname=cfg.server if cfg.tls else None), CONNECT_TIMEOUT)
        self.connections += 1
        connected_at = time.monotonic()
        log.info("Connected to %s:%d as %s", cfg.server, cfg.port, cfg.name)
        eof = asyncio.ensure_future(reader.read(1))
        stopping = asyncio.ensure_future(self.stop_event.wait())
        try:
            for line in ({"token": cfg.token}, get_specs(cfg.name, cfg.interval)):
                writer.write((json.dumps(line) + "\n").encode("utf-8"))
            await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)
            while not self.stop_event.is_set():
                if eof.done():
                    lasted = time.monotonic() - connected_at
                    if lasted < REJECT_WINDOW:
                        raise ConnectionError("the server closed the connection right away: token unknown or "
                                              "revoked, or the name does not match the token")
                    raise ConnectionError("the server closed the connection")
                writer.write((json.dumps(self.collector.sample()) + "\n").encode("utf-8"))
                await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)
                await asyncio.wait({eof, stopping}, timeout=cfg.interval, return_when=asyncio.FIRST_COMPLETED)
            return time.monotonic() - connected_at
        finally:
            eof.cancel()
            stopping.cancel()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001  (the peer may already be gone)
                pass
            self.last_session = time.monotonic() - connected_at

    async def run(self):
        cfg = self.config
        delays = backoff_delays(cfg.backoff_base, cfg.backoff_max, rng=self.rng)
        while not self.stop_event.is_set():
            self.last_session = 0.0
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError, asyncio.TimeoutError, ssl.SSLError) as exc:
                if self.last_session >= STABLE_AFTER:
                    delays = backoff_delays(cfg.backoff_base, cfg.backoff_max, rng=self.rng)
                delay = next(delays)
                level = logging.ERROR if "right away" in str(exc) else logging.WARNING
                log.log(level, "%s; retrying in %.1fs", exc or exc.__class__.__name__, delay)
                await self._sleep(delay)
        log.info("Stopped")


# --- entry point -----------------------------------------------------------------------------------------------

async def _amain(config):
    agent = Agent(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, agent.stop)
        except NotImplementedError:  # Windows
            pass
    await agent.run()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="observer-agent", description=__doc__.split("\n")[0])
    parser.add_argument("--config", help="TOML configuration file (or OBSERVER_AGENT_CONFIG)")
    parser.add_argument("--check", action="store_true", help="validate the configuration and exit")
    args = parser.parse_args(argv)
    try:
        config = load_config(config_path=args.config)
        validate_nic(config.nic)
    except AgentConfigError as exc:
        print("observer-agent: " + str(exc), file=sys.stderr)
        return 2
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.check:
        print("configuration ok: %s -> %s:%d" % (config.name, config.server, config.port))
        return 0
    try:
        asyncio.run(_amain(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
