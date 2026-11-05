"""HELIA_ARCHIVE_ENABLED: archiving to Helia is opt-in and, off by default, changes nothing about retention.

There is no archive job yet (only the unwired client); the flag is the gate it will have to sit behind. These tests pin
the contract so that wiring one in later cannot change today's behaviour for anyone who has not turned it on.
"""
import ast
import os
import pathlib
import subprocess
import sys

import pytest

import config
import db as db_module

ROOT = pathlib.Path(__file__).resolve().parent.parent


def flag_with_env(value):
    """What a fresh process reads from config when HELIA_ARCHIVE_ENABLED is `value` (None = unset)."""
    env = {k: v for k, v in os.environ.items() if k != "HELIA_ARCHIVE_ENABLED"}
    if value is not None:
        env["HELIA_ARCHIVE_ENABLED"] = value
    out = subprocess.run([sys.executable, "-c", "import config; print(config.HELIA_ARCHIVE_ENABLED)"],
                         cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_archiving_is_off_when_the_variable_is_unset():
    assert flag_with_env(None) == "False"


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off", "enabled", "y", "2", " "])
def test_anything_that_is_not_a_clear_yes_is_off(value):
    """A typo must not turn archiving on."""
    assert flag_with_env(value) == "False"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
def test_a_clear_yes_turns_it_on(value):
    assert flag_with_env(value) == "True"


async def _ttls(db):
    out = {}
    for name in ("events", "loadtests"):
        info = {spec["name"]: spec async for spec in await db[name].list_indexes()}
        out[name] = {n: s.get("expireAfterSeconds") for n, s in info.items() if "expireAfterSeconds" in s}
    return out


@pytest.mark.parametrize("enabled", [False, True])
async def test_retention_indexes_are_the_same_whether_or_not_archiving_is_enabled(db, monkeypatch, enabled):
    monkeypatch.setattr(config, "HELIA_ARCHIVE_ENABLED", enabled)
    await db_module.ensure_indexes()
    ttls = await _ttls(db)
    assert list(ttls["events"].values()) == [config.EVENT_RETENTION_DAYS * 86400]
    assert list(ttls["loadtests"].values()) == [config.LOADTEST_RETENTION_DAYS * 86400]


def test_nothing_in_the_application_imports_the_helia_client_yet():
    """Unwired means unwired: with the flag on or off, no process can reach Helia."""
    for path in ROOT.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n.split(".")[0] == "integrations" for n in names), "%s imports integrations" % path.name


def test_the_variable_is_documented_and_defaults_to_off():
    lines = (ROOT / ".env.example").read_text().splitlines()
    assert "HELIA_ARCHIVE_ENABLED=false" in lines
