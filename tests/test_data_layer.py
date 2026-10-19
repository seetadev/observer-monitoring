import ast
import asyncio
import json
import pathlib

import pytest
from pymongo import AsyncMongoClient

import auth
import config
import db as db_module
import root
from tests.conftest import make_stat

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPECS = {"machine": "m", "node": "n", "architecture": ["64bit", ""], "system": "Linux", "release": "r",
         "version": "v", "name": "box", "memory": 123}


async def index_info(collection):
    return {spec["name"]: spec async for spec in await collection.list_indexes()}


# --- driver ---------------------------------------------------------------------------------------------------

def test_the_native_async_client_is_used_and_motor_is_gone():
    imported = set()
    for path in ROOT.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    assert "motor" not in imported
    assert "motor" not in (ROOT / "requirements.txt").read_text()


async def test_get_db_returns_an_async_pymongo_database(db):
    assert isinstance(db.client, AsyncMongoClient)


async def test_close_db_allows_a_fresh_client(db):
    first = db_module.get_db().client
    await db_module.close_db()
    assert db_module.get_db().client is not first


# --- indexes --------------------------------------------------------------------------------------------------

async def test_fixed_collections_get_unique_indexes(db):
    await db_module.ensure_indexes()
    await db_module.ensure_indexes()  # idempotent
    machines, agents = await index_info(db["machines"]), await index_info(db["agents"])
    assert machines["name_1"]["unique"] is True
    assert agents["token_sha256_1"]["unique"] and agents["name_1"]["unique"]


async def test_duplicate_machine_names_are_refused_once_indexed(db):
    await db_module.ensure_indexes()
    await db["machines"].insert_one({"name": "box"})
    with pytest.raises(Exception, match="duplicate key"):
        await db["machines"].insert_one({"name": "box"})


async def test_ensure_indexes_survives_existing_duplicates(db):
    await db["machines"].insert_many([{"name": "box"}, {"name": "box"}])
    await db_module.ensure_indexes()


async def test_per_machine_indexes_and_ttl(db, monkeypatch):
    monkeypatch.setattr(config, "LIVE_TTL_SECONDS", 600)
    monkeypatch.setattr(config, "STAT_RETENTION_DAYS", 0)
    await db_module.ensure_machine_indexes("box")
    live, stat = await index_info(db["[box]-live"]), await index_info(db["[box]-stat"])
    assert live["ts_1"]["expireAfterSeconds"] == 600
    assert "date_1" in stat and "ts_1" not in stat


async def test_stat_retention_adds_a_ttl_index(db, monkeypatch):
    monkeypatch.setattr(config, "STAT_RETENTION_DAYS", 30)
    await db_module.ensure_machine_indexes("box")
    assert (await index_info(db["[box]-stat"]))["ts_1"]["expireAfterSeconds"] == 30 * 86400


async def test_changing_the_configured_ttl_updates_the_index(db, monkeypatch):
    monkeypatch.setattr(config, "LIVE_TTL_SECONDS", 600)
    await db_module.ensure_machine_indexes("box")
    db_module.forget_machine("box")
    monkeypatch.setattr(config, "LIVE_TTL_SECONDS", 1200)
    await db_module.ensure_machine_indexes("box")
    assert (await index_info(db["[box]-live"]))["ts_1"]["expireAfterSeconds"] == 1200


async def test_disabling_the_ttl_removes_the_index(db, monkeypatch):
    monkeypatch.setattr(config, "LIVE_TTL_SECONDS", 600)
    await db_module.ensure_machine_indexes("box")
    db_module.forget_machine("box")
    monkeypatch.setattr(config, "LIVE_TTL_SECONDS", 0)
    await db_module.ensure_machine_indexes("box")
    assert "ts_1" not in await index_info(db["[box]-live"])


async def test_deleting_a_machine_makes_its_indexes_come_back_on_the_next_write(dash, db):
    await db_module.ensure_machine_indexes("box")
    await db["machines"].insert_one({"name": "box"})
    await dash.post("/api", data={"action": "DELETE_REMOTE_MACHINE", "machine_id": "box"})
    assert "[box]-stat" not in await db.list_collection_names()
    await db_module.ensure_machine_indexes("box")
    assert "date_1" in await index_info(db["[box]-stat"])


# --- timestamps and stored shape ------------------------------------------------------------------------------

@pytest.fixture
async def listener(db):
    from tornado.testing import bind_unused_port
    sock, port = bind_unused_port()
    server = root.TCPListener()
    server.add_sockets([sock])
    yield port
    server.stop()


async def feed(port, token, lines):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(("".join(json.dumps(x) + "\n" for x in [{"token": token}] + lines)).encode())
    await writer.drain()
    await asyncio.sleep(1.0)
    writer.close()


async def test_stored_records_carry_a_utc_timestamp_and_indexes_exist(db, listener):
    token = await auth.create_agent("box")
    await feed(listener, token, [SPECS, make_stat()])
    live = await db["[box]-live"].find_one()
    assert live["ts"].year >= 2026
    assert "ts_1" in await index_info(db["[box]-live"])


async def test_condensed_records_carry_a_timestamp(db, listener):
    token = await auth.create_agent("box")
    await feed(listener, token, [SPECS] + [make_stat() for _ in range(121)])
    stat = await db["[box]-stat"].find_one()
    assert stat is not None and stat["ts"] is not None


async def test_first_registration_stores_last_online_like_later_updates(db, listener):
    token = await auth.create_agent("box")
    await feed(listener, token, [SPECS])
    assert (await db["machines"].find_one({"name": "box"}))["last_online"].isdigit()


# --- fixed defects --------------------------------------------------------------------------------------------

async def test_unknown_pricing_provider_is_404(dash):
    response = await dash.post("/api", data={"action": "FETCH_INSTANCE_PRICING", "provider_name": "nope"})
    assert response.status_code == 404


async def test_stat_data_is_returned_in_insertion_order(dash, db):
    await db["[box]-stat"].insert_many([make_stat(ram=float(v)) for v in (3, 1, 2)])
    body = (await dash.post("/api", data={"action": "GET_STAT_DATA", "client-name": "box"})).json()
    assert [r["ram"] for r in body["stat_data"]] == [3.0, 1.0, 2.0]


async def test_stat_data_does_not_leak_mongo_object_ids_as_objects(dash, db):
    await db["[box]-stat"].insert_one(make_stat())
    body = (await dash.post("/api", data={"action": "GET_STAT_DATA", "client-name": "box"})).json()
    assert isinstance(body["stat_data"][0]["_id"], str)
