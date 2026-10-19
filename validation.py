"""Validation of what agents send. Records are rebuilt from known fields only."""
import math

import auth

MAX_TEXT = 200
MAX_CPUS = 1024

SPEC_TEXT_FIELDS = ("machine", "node", "system", "release", "version")
SCALAR_STATS = ("cpu_count", "cpu_ctx_switches", "cpu_interrupts", "ram", "ram-available", "ram-used", "swap",
                "swap-total", "swap-used", "disk_io_read", "disk_io_write", "disk_total", "disk_used", "uptime",
                "bytes_recv", "bytes_sent", "dl_rate", "ul_rate", "disk_read_rate", "disk_write_rate")
PAIR_STATS = ("packets_sent", "packets_recv")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("expected a finite number")
    return value


def _text(value):
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ValueError("expected short text")
    return value


def valid_name(value):
    """True for names that are safe in collection names, file names and the page."""
    return isinstance(value, str) and auth.NAME_PATTERN.match(value) is not None


def clean_specs(record):
    architecture = record.get("architecture")
    if not isinstance(architecture, list) or len(architecture) > 2:
        raise ValueError("bad architecture")
    cleaned = {field: _text(record.get(field)) for field in SPEC_TEXT_FIELDS}
    cleaned.update(name=record["name"], node=cleaned["node"], architecture=[_text(a) for a in architecture],
                   memory=_number(record.get("memory")))
    if record.get("interval") is not None:
        interval = _number(record["interval"])
        if not 0.1 <= interval <= 3600:
            raise ValueError("bad interval")
        cleaned["interval"] = interval
    if not valid_name(cleaned["name"]):
        raise ValueError("bad name")
    return cleaned


def clean_stats(record):
    if not valid_name(record.get("name")):
        raise ValueError("bad name")
    cleaned = {"name": record["name"], "date": _text(record.get("date"))}
    for field in SCALAR_STATS:
        cleaned[field] = _number(record.get(field))
    cpu = record.get("cpu")
    if not isinstance(cpu, list) or not 1 <= len(cpu) <= MAX_CPUS:
        raise ValueError("bad cpu list")
    cleaned["cpu"] = [_number(v) for v in cpu]
    for field in PAIR_STATS:
        pair = record.get(field)
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("bad packet counters")
        cleaned[field] = [_number(v) for v in pair]
    return cleaned


def clean_record(record):
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    return clean_specs(record) if "node" in record else clean_stats(record)
