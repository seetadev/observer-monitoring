import sys

import pytest

import remote

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="agent reads /proc")


@pytest.fixture(autouse=True)
def reset_counters(monkeypatch):
    for name in ("prev_recv", "prev_sent", "prev_read", "prev_write"):
        monkeypatch.setattr(remote, name, -1)


def _nic():
    import psutil
    return next(iter(psutil.net_io_counters(pernic=True)))


def test_get_stats_has_the_documented_fields():
    stats = remote.get_stats("box", _nic())
    for key in ("date", "name", "cpu", "cpu_count", "ram", "disk_io_read", "disk_io_write", "disk_total",
                "disk_used", "uptime", "packets_sent", "packets_recv", "bytes_recv", "bytes_sent", "dl_rate",
                "ul_rate", "disk_read_rate", "disk_write_rate"):
        assert key in stats
    assert stats["name"] == "box"
    assert isinstance(stats["cpu"], list) and len(stats["cpu"]) == stats["cpu_count"]


def test_first_sample_has_zero_network_and_read_rates():
    stats = remote.get_stats("box", _nic())
    assert stats["dl_rate"] == 0 and stats["ul_rate"] == 0 and stats["disk_read_rate"] == 0


def test_first_sample_has_zero_disk_write_rate():
    assert remote.get_stats("box", _nic())["disk_write_rate"] == 0


def test_get_specs_is_json_serialisable_and_identifies_the_machine():
    import json
    specs = remote.get_specs("box")
    json.dumps(specs)
    assert specs["name"] == "box" and "node" in specs and specs["memory"] > 0
