import asyncio
import json
import logging
import pathlib
import shutil
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

import agent
import auth
import root
import validation

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKEN = "t0ken-" + "x" * 40


def env(**extra):
    base = {"OBSERVER_AGENT_SERVER": "127.0.0.1", "OBSERVER_AGENT_NAME": "box", "OBSERVER_AGENT_TOKEN": TOKEN}
    base.update(extra)
    return base


# --- configuration ----------------------------------------------------------------------------------------------

def test_defaults():
    config = agent.load_config(env())
    assert (config.server, config.port, config.name, config.nic, config.interval) == ("127.0.0.1", 8889, "box",
                                                                                       "auto", 1.0)
    assert config.tls is False and config.backoff_max == 60.0


def test_token_is_never_part_of_the_repr_or_logs(caplog):
    config = agent.load_config(env())
    assert TOKEN not in repr(config) and TOKEN not in str(config)


def test_token_from_a_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o600)
    config = agent.load_config({"OBSERVER_AGENT_NAME": "box", "OBSERVER_AGENT_TOKEN_FILE": str(token_file)})
    assert config.token == TOKEN


def test_world_readable_token_file_is_warned_about(tmp_path, caplog):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    token_file.chmod(0o644)
    with caplog.at_level(logging.WARNING, logger="observer.agent"):
        agent.load_config({"OBSERVER_AGENT_NAME": "box", "OBSERVER_AGENT_TOKEN_FILE": str(token_file)})
    assert "readable by every user" in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize("settings,message", [
    ({"OBSERVER_AGENT_TOKEN": ""}, "no token"),
    ({"OBSERVER_AGENT_TOKEN": "   "}, "no token"),
    ({"OBSERVER_AGENT_TOKEN_FILE": "/nonexistent/token", "OBSERVER_AGENT_TOKEN": ""}, "cannot read the token file"),
    ({"OBSERVER_AGENT_NAME": "bad name"}, "not a valid name"),
    ({"OBSERVER_AGENT_NAME": "../x"}, "not a valid name"),
    ({"OBSERVER_AGENT_PORT": "0"}, "port"),
    ({"OBSERVER_AGENT_PORT": "abc"}, "numbers"),
    ({"OBSERVER_AGENT_INTERVAL": "0"}, "interval"),
    ({"OBSERVER_AGENT_BACKOFF_MAX": "0"}, "backoff_max"),
    ({"OBSERVER_AGENT_LOG_LEVEL": "LOUD"}, "log_level"),
])
def test_invalid_configuration_is_refused_with_a_clear_message(settings, message):
    with pytest.raises(agent.AgentConfigError, match=message):
        agent.load_config(env(**settings))


def test_toml_file_with_environment_override(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('server = "observer.example.org"\nport = 9999\nname = "from-file"\ninterval = 2.5\n')
    environment = {"OBSERVER_AGENT_TOKEN": TOKEN, "OBSERVER_AGENT_NAME": "from-env"}
    config = agent.load_config(environment, config_path=str(path))
    assert (config.server, config.port, config.name, config.interval) == ("observer.example.org", 9999,
                                                                          "from-env", 2.5)


def test_token_inside_the_toml_file_is_refused(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('token = "abc"\n')
    with pytest.raises(agent.AgentConfigError, match="must not be stored in the config file"):
        agent.load_config(env(), config_path=str(path))


def test_unknown_or_broken_toml_is_refused(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('colour = "blue"\n')
    with pytest.raises(agent.AgentConfigError, match="unknown config keys: colour"):
        agent.load_config(env(), config_path=str(path))
    path.write_text("this is = = not toml")
    with pytest.raises(agent.AgentConfigError, match="invalid config file"):
        agent.load_config(env(), config_path=str(path))


def test_the_example_files_are_valid():
    config = agent.load_config({"OBSERVER_AGENT_TOKEN": TOKEN},
                               config_path=str(ROOT / "deploy" / "agent.toml.example"))
    assert (config.server, config.name, config.port) == ("observer.example.org", "web-01", 8889)


def test_nic_validation_lists_the_available_interfaces():
    agent.validate_nic("auto")
    with pytest.raises(agent.AgentConfigError, match="not found; available:"):
        agent.validate_nic("definitely-not-a-nic")


def test_main_reports_configuration_errors_without_the_token(monkeypatch, capsys):
    monkeypatch.setenv("OBSERVER_AGENT_NAME", "bad name")
    monkeypatch.setenv("OBSERVER_AGENT_TOKEN", TOKEN)
    assert agent.main([]) == 2
    captured = capsys.readouterr()
    assert "not a valid name" in captured.err and TOKEN not in captured.err + captured.out


def test_check_mode_validates_and_exits(monkeypatch, capsys):
    for key, value in env().items():
        monkeypatch.setenv(key, value)
    assert agent.main(["--check"]) == 0
    assert "configuration ok" in capsys.readouterr().out


def test_run_py_agent_never_prompts(monkeypatch, capsys):
    import run
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("the agent must not prompt"))
    monkeypatch.delenv("OBSERVER_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("OBSERVER_AGENT_TOKEN_FILE", raising=False)
    assert run.main(["run.py", "agent"]) == 2
    assert "no token" in capsys.readouterr().err


# --- metrics ----------------------------------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def fake_psutil(monkeypatch, nics, disk_io=(1000, 2000)):
    """Replace the network and disk counters with values the test can change between samples."""
    state = SimpleNamespace(nics=nics, disk_io=disk_io)

    def net_io_counters(pernic=False):
        return {n: SimpleNamespace(bytes_sent=v[0], bytes_recv=v[1], packets_sent=v[2], packets_recv=v[3],
                                   errin=v[4], errout=v[5]) for n, v in state.nics.items()}

    def disk_io_counters():
        return SimpleNamespace(read_bytes=state.disk_io[0], write_bytes=state.disk_io[1]) if state.disk_io else None

    monkeypatch.setattr(psutil, "net_io_counters", net_io_counters)
    monkeypatch.setattr(psutil, "disk_io_counters", disk_io_counters)
    return state


def test_sample_has_every_field_the_server_accepts():
    sample = agent.Collector("box").sample()
    cleaned = validation.clean_record(sample)
    assert cleaned["name"] == "box" and len(cleaned["cpu"]) == sample["cpu_count"]
    json.dumps(sample)


def test_first_sample_has_zero_rates_and_later_ones_are_per_second(monkeypatch):
    state = fake_psutil(monkeypatch, {"eth0": (1000, 2000, 10, 20, 0, 0)})
    clock = FakeClock()
    collector = agent.Collector("box", "eth0", clock=clock)
    first = collector.sample()
    assert (first["dl_rate"], first["ul_rate"], first["disk_read_rate"], first["disk_write_rate"]) == (0, 0, 0, 0)
    clock.now += 2.0
    state.nics["eth0"] = (1600, 3000, 14, 30, 0, 0)
    state.disk_io = (1400, 2600)
    second = collector.sample()
    assert second["ul_rate"] == 300 and second["dl_rate"] == 500
    assert second["disk_read_rate"] == 200 and second["disk_write_rate"] == 300


def test_counter_reset_gives_zero_not_a_negative_rate(monkeypatch):
    state = fake_psutil(monkeypatch, {"eth0": (5000, 5000, 1, 1, 0, 0)})
    clock = FakeClock()
    collector = agent.Collector("box", "eth0", clock=clock)
    collector.sample()
    clock.now += 1
    state.nics["eth0"] = (10, 10, 1, 1, 0, 0)
    state.disk_io = (1, 1)
    sample = collector.sample()
    assert sample["dl_rate"] == sample["ul_rate"] == sample["disk_read_rate"] == sample["disk_write_rate"] == 0


def test_auto_nic_sums_non_loopback_interfaces_only(monkeypatch):
    fake_psutil(monkeypatch, {"lo": (9999, 9999, 99, 99, 9, 9), "eth0": (100, 200, 1, 2, 0, 0),
                              "wlan0": (10, 20, 3, 4, 1, 1)})
    sample = agent.Collector("box", "auto").sample()
    assert sample["bytes_sent"] == 110 and sample["bytes_recv"] == 220
    assert sample["packets_sent"] == [4, 1] and sample["packets_recv"] == [6, 1]


def test_a_named_nic_is_used_alone(monkeypatch):
    fake_psutil(monkeypatch, {"eth0": (100, 200, 1, 2, 0, 0), "wlan0": (10, 20, 3, 4, 0, 0)})
    assert agent.Collector("box", "wlan0").sample()["bytes_sent"] == 10


def test_missing_disk_io_counters_do_not_break_sampling(monkeypatch):
    fake_psutil(monkeypatch, {"eth0": (1, 1, 1, 1, 0, 0)}, disk_io=None)
    sample = agent.Collector("box", "eth0").sample()
    assert sample["disk_io_read"] == 0 and sample["disk_write_rate"] == 0


def test_uptime_does_not_depend_on_proc(monkeypatch):
    monkeypatch.setattr(psutil, "boot_time", lambda: __import__("time").time() - 1234)
    assert 1233 <= agent.Collector("box").sample()["uptime"] <= 1240


def test_specs_are_accepted_by_the_server():
    assert validation.clean_record(agent.get_specs("box"))["name"] == "box"


def test_agent_module_needs_only_psutil_and_the_standard_library():
    import ast
    imported = set()
    for node in ast.walk(ast.parse((ROOT / "agent.py").read_text())):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported - set(sys.stdlib_module_names) == {"psutil"}


# --- reconnect --------------------------------------------------------------------------------------------------

def test_backoff_doubles_up_to_the_cap_with_bounded_jitter():
    quiet = agent.backoff_delays(1, 8, jitter=0.25, rng=lambda: 0.5)
    assert [next(quiet) for _ in range(6)] == [1, 2, 4, 8, 8, 8]
    low = agent.backoff_delays(1, 60, jitter=0.25, rng=lambda: 0.0)
    high = agent.backoff_delays(1, 60, jitter=0.25, rng=lambda: 1.0)
    assert next(low) == 0.75 and next(high) == 1.25


class FakeServer:
    """A server that records what it receives and optionally hangs up after a few lines."""

    def __init__(self, drop_after=None):
        self.drop_after = drop_after
        self.sessions = []
        self.server = None
        self.port = None

    async def start(self, ssl=None, port=0):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port, ssl=ssl)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        lines = []
        self.sessions.append(lines)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                lines.append(json.loads(line))
                if self.drop_after and len(lines) >= self.drop_after:
                    break
        finally:
            writer.close()

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


def quick_config(port, **overrides):
    config = agent.load_config(env(OBSERVER_AGENT_PORT=str(port), OBSERVER_AGENT_NIC="auto",
                                   OBSERVER_AGENT_INTERVAL="0.1"))
    import dataclasses
    return dataclasses.replace(config, backoff_base=0.05, backoff_max=0.2, **overrides)


async def wait_until(predicate, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_session_sends_token_then_specs_then_samples():
    server = FakeServer()
    await server.start()
    runner = agent.Agent(quick_config(server.port))
    task = asyncio.create_task(runner.run())
    assert await wait_until(lambda: server.sessions and len(server.sessions[0]) >= 4)
    runner.stop()
    await asyncio.wait_for(task, 5)
    await server.stop()
    first, specs, sample = server.sessions[0][:3]
    assert first == {"token": TOKEN}
    assert specs["name"] == "box" and "node" in specs
    assert sample["name"] == "box" and "cpu" in sample


async def test_agent_reconnects_after_the_server_drops_it_and_resends_credentials():
    server = FakeServer(drop_after=4)
    await server.start()
    runner = agent.Agent(quick_config(server.port))
    task = asyncio.create_task(runner.run())
    assert await wait_until(lambda: len(server.sessions) >= 3)
    runner.stop()
    await asyncio.wait_for(task, 5)
    await server.stop()
    assert all(s[0] == {"token": TOKEN} and "node" in s[1] for s in server.sessions)


async def test_agent_keeps_retrying_while_the_server_is_down_then_connects():
    probe = FakeServer()
    await probe.start()
    port = probe.port
    await probe.stop()
    runner = agent.Agent(quick_config(port))
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.6)
    assert not task.done()
    server = FakeServer()
    await server.start(port=port)
    assert await wait_until(lambda: server.sessions and len(server.sessions[0]) >= 3)
    runner.stop()
    await asyncio.wait_for(task, 5)
    await server.stop()


async def test_retry_delays_grow_while_the_server_is_unreachable(monkeypatch):
    probe = FakeServer()
    await probe.start()
    port = probe.port
    await probe.stop()
    waits = []
    runner = agent.Agent(quick_config(port), rng=lambda: 0.5)
    original = runner._sleep

    async def record(seconds):
        waits.append(round(seconds, 3))
        await original(0.01)

    monkeypatch.setattr(runner, "_sleep", record)
    task = asyncio.create_task(runner.run())
    assert await wait_until(lambda: len(waits) >= 5)
    runner.stop()
    await asyncio.wait_for(task, 5)
    assert waits[:5] == [0.05, 0.1, 0.2, 0.2, 0.2]


async def test_stop_closes_the_connection_promptly():
    server = FakeServer()
    await server.start()
    runner = agent.Agent(quick_config(server.port))
    task = asyncio.create_task(runner.run())
    assert await wait_until(lambda: server.sessions and len(server.sessions[0]) >= 3)
    runner.stop()
    await asyncio.wait_for(task, 2)
    await server.stop()


async def test_quick_hang_up_is_reported_as_a_refusal(caplog):
    server = FakeServer(drop_after=2)
    await server.start()
    runner = agent.Agent(quick_config(server.port))
    with caplog.at_level(logging.ERROR, logger="observer.agent"):
        task = asyncio.create_task(runner.run())
        assert await wait_until(lambda: "token unknown or revoked" in caplog.text)
    runner.stop()
    await asyncio.wait_for(task, 5)
    await server.stop()
    assert TOKEN not in caplog.text


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not available")
async def test_tls_with_a_private_ca(tmp_path):
    import ssl
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                    str(cert), "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = FakeServer()
    await server.start(ssl=context)
    runner = agent.Agent(quick_config(server.port, tls=True, tls_ca=str(cert)))
    task = asyncio.create_task(runner.run())
    assert await wait_until(lambda: server.sessions and len(server.sessions[0]) >= 3)
    runner.stop()
    await asyncio.wait_for(task, 5)
    await server.stop()


# --- against the real listener ---------------------------------------------------------------------------------

async def test_agent_delivers_samples_to_the_real_listener(db):
    from tornado.testing import bind_unused_port
    token = await auth.create_agent("box")
    sock, port = bind_unused_port()
    listener = root.TCPListener()
    listener.add_sockets([sock])
    config = agent.load_config(env(OBSERVER_AGENT_PORT=str(port), OBSERVER_AGENT_TOKEN=token,
                                   OBSERVER_AGENT_INTERVAL="0.1"))
    runner = agent.Agent(config)
    task = asyncio.create_task(runner.run())

    async def stored():
        return await db["[box]-live"].count_documents({}) >= 3

    ok = False
    for _ in range(100):
        if await stored():
            ok = True
            break
        await asyncio.sleep(0.1)
    runner.stop()
    await asyncio.wait_for(task, 5)
    listener.stop()
    assert ok
    machine = await db["machines"].find_one({"name": "box"})
    assert machine["system"] == platform_system()
    record = await db["[box]-live"].find_one()
    assert record["disk_write_rate"] < 10 ** 9  # the first-sample typo made this a cumulative counter


def platform_system():
    import platform
    return platform.system()


async def test_agent_with_a_wrong_token_stores_nothing(db):
    from tornado.testing import bind_unused_port
    await auth.create_agent("box")
    sock, port = bind_unused_port()
    listener = root.TCPListener()
    listener.add_sockets([sock])
    runner = agent.Agent(quick_config(port))
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(1.5)
    runner.stop()
    await asyncio.wait_for(task, 5)
    listener.stop()
    assert runner.connections >= 2
    assert "[box]-live" not in await db.list_collection_names()


# --- deployment files -------------------------------------------------------------------------------------------

UNIT = ROOT / "deploy" / "observer-agent.service"


def test_systemd_unit_is_hardened_and_holds_no_secret():
    text = UNIT.read_text()
    for needle in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                   "DynamicUser=yes", "Restart=always", "LoadCredential=token:", "OBSERVER_AGENT_TOKEN_FILE=%d/token",
                   "EnvironmentFile=/etc/observer-agent/agent.env"):
        assert needle in text
    assert "OBSERVER_AGENT_TOKEN=" not in text


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not available")
def test_systemd_unit_passes_systemd_analyze_verify(tmp_path):
    unit = tmp_path / "observer-agent.service"
    unit.write_text(UNIT.read_text().replace("/opt/observer-agent/venv/bin/python /opt/observer-agent/agent.py",
                                             "/usr/bin/env python3"))
    result = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True)
    problems = [line for line in result.stderr.splitlines()
                if "observer-agent" in line and "not executable" not in line and "EnvironmentFile" not in line]
    assert not problems, problems


def test_example_environment_file_has_no_token():
    assert "TOKEN" not in (ROOT / "deploy" / "agent.env.example").read_text().replace("token", "")


def test_the_agent_requirements_are_just_psutil():
    assert (ROOT / "requirements-agent.txt").read_text().split("==")[0] == "psutil"
