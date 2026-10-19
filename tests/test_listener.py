import asyncio
import json

import pytest

from tornado.testing import bind_unused_port

import root
from tests.conftest import make_stat

SPECS = {"machine": "m", "node": "n", "architecture": ["64bit", ""], "system": "Linux", "release": "r",
         "version": "v", "name": "box", "memory": 123}


async def _send(port, lines, token=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    if token is not None:
        writer.write((json.dumps({"token": token}) + "\n").encode())
    for line in lines:
        writer.write((json.dumps(line) + "\n").encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _wait_for(predicate, timeout=20):
    for _ in range(int(timeout / 0.1)):
        if await predicate():
            return True
        await asyncio.sleep(0.1)
    return False


@pytest.fixture
async def token(db):
    import auth
    return await auth.create_agent("box")


async def _listener():
    sock, port = bind_unused_port()
    server = root.TCPListener()
    server.add_sockets([sock])
    return server, port


async def test_specs_line_registers_the_machine(db, token):
    server, port = await _listener()
    try:
        await _send(port, [SPECS], token)
        assert await _wait_for(lambda: db["machines"].find_one({"name": "box"}))
        machine = await db["machines"].find_one({"name": "box"})
        assert machine["ip"] == "127.0.0.1" and machine["memory"] == 123
    finally:
        server.stop()


async def test_stat_lines_are_stored_and_update_last_online(db, token):
    server, port = await _listener()
    try:
        await _send(port, [SPECS, make_stat()], token)
        async def stored():
            return await db["[box]-live"].count_documents({}) == 1
        assert await _wait_for(stored)
        machine = await db["machines"].find_one({"name": "box"})
        assert machine["last_online"].isdigit()
    finally:
        server.stop()


async def test_live_buffer_is_condensed_into_one_minute_stat_records(db, token):
    server, port = await _listener()
    try:
        await _send(port, [SPECS] + [make_stat(ram=float(i % 2) * 100) for i in range(121)], token)
        async def condensed():
            return await db["[box]-stat"].count_documents({}) == 1
        assert await _wait_for(condensed)
        stat = await db["[box]-stat"].find_one()
        assert stat["ram"] == 50.0 and stat["name"] == "box"
        assert await db["[box]-live"].count_documents({}) == 61
    finally:
        server.stop()


async def test_pricing_seed_is_idempotent(db):
    await root.init_instance_pricing()
    await root.init_instance_pricing()
    assert await db["cloud-pricing"].count_documents({"name": "amazon-ec2"}) == 1
