"""MongoDB access (PyMongo native async client). One client per process, created lazily inside the running loop.

All database access in the application goes through get_db(); the helpers below own index management.
"""
import logging

from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError, OperationFailure

import config

log = logging.getLogger('observer')

_client = None
_indexed_machines = set()


def get_db():
    global _client
    if _client is None:
        _client = AsyncMongoClient(config.MONGO_URL)
    return _client[config.DB_NAME]


async def close_db():
    """Close and forget the cached client (tests create a new event loop per test)."""
    global _client
    _indexed_machines.clear()
    if _client is not None:
        client, _client = _client, None
        await client.close()


def live_name(machine):
    return "[" + machine + "]-live"


def stat_name(machine):
    return "[" + machine + "]-stat"


async def _ttl_index(collection, seconds):
    """Keep a TTL index on `ts` in line with the configured lifetime (0 = keep forever)."""
    try:
        existing = {name: spec async for name, spec in _index_specs(collection)}
    except OperationFailure:
        existing = {}
    current = existing.get("ts_1", {}).get("expireAfterSeconds")
    if seconds <= 0:
        if "ts_1" in existing and current is not None:
            await collection.drop_index("ts_1")
        return
    if "ts_1" not in existing:
        await collection.create_index("ts", name="ts_1", expireAfterSeconds=seconds)
    elif current != seconds:
        await collection.database.command({"collMod": collection.name, "index": {
            "keyPattern": {"ts": 1}, "expireAfterSeconds": seconds}})


async def _index_specs(collection):
    async for spec in await collection.list_indexes():
        yield spec["name"], spec


async def ensure_indexes():
    """Indexes for the fixed collections. Safe to call from every process, repeatedly."""
    database = get_db()
    try:
        await database["machines"].create_index("name", unique=True)
    except DuplicateKeyError:
        log.warning("machines contains duplicate names; the unique index was not created")
    await database["agents"].create_index("token_sha256", unique=True)
    await database["agents"].create_index("name", unique=True)
    await database["states"].create_index([("kind", 1), ("name", 1)])
    await _ttl_index(database["events"], config.EVENT_RETENTION_DAYS * 86400)
    await database["events"].create_index("time")


async def ensure_machine_indexes(machine):
    """Per-machine collections are created on first write; index them once per process.

    live: TTL on `ts` so a buffer left behind by a stopped agent disappears.
    stat: `date` for the first/last record look-ups; optional TTL for long-term retention.
    """
    if machine in _indexed_machines:
        return
    database = get_db()
    await _ttl_index(database[live_name(machine)], config.LIVE_TTL_SECONDS)
    await database[stat_name(machine)].create_index("date")
    await _ttl_index(database[stat_name(machine)], config.STAT_RETENTION_DAYS * 86400)
    _indexed_machines.add(machine)


def forget_machine(machine):
    """Call after dropping a machine's collections so their indexes are recreated on the next write."""
    _indexed_machines.discard(machine)
