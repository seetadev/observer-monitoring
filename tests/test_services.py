import pytest

import root
from tests.conftest import make_stat


async def test_live_returns_the_latest_record(serve, db):
    client = serve(root.make_live_app())
    await db["[box]-live"].insert_many([make_stat(ram=1.0), make_stat(ram=2.0)])
    body = (await client.post("/live", data={"action": "GET_LIVE_DATA", "client-name": "box"})).json()
    assert body["ram"] == 2.0 and isinstance(body["_id"], str)


async def test_live_requires_an_action(serve):
    client = serve(root.make_live_app())
    assert (await client.post("/live", data={})).status_code == 400


async def test_compute_cost_stats(serve, db):
    client = serve(root.make_compute_app())
    await db["[box]-stat"].insert_many([make_stat(date="2026-01-01T00:00:00"), make_stat(date="2026-01-01T01:00:00")])
    body = (await client.post("/compute", data={"action": "GET_COST_STATS", "machine": "box"})).json()
    assert body == {"duration": 60.0, "count": 2}


@pytest.mark.xfail(strict=True, reason="no stat data currently produces HTTP 500")
async def test_compute_cost_stats_without_data_is_not_a_server_error(serve):
    client = serve(root.make_compute_app())
    response = await client.post("/compute", data={"action": "GET_COST_STATS", "machine": "ghost"})
    assert response.status_code < 500


async def test_estimated_charts_need_enough_data(serve, db):
    client = serve(root.make_compute_app())
    await db["[box]-stat"].insert_many([make_stat() for _ in range(2)])
    body = (await client.post("/compute", data={"action": "FETCH_ESTIMATED_CHARTS", "machine": "box",
                                                "days_duration": "3"})).json()
    assert body["status"] == "failure"


async def test_estimated_charts_extrapolate_to_the_requested_duration(serve, db):
    client = serve(root.make_compute_app())
    await db["[box]-stat"].insert_many(
        [make_stat(date="2026-01-01T00:0%d:00" % i) for i in range(6)])
    body = (await client.post("/compute", data={"action": "FETCH_ESTIMATED_CHARTS", "machine": "box",
                                                "days_duration": "2"})).json()
    assert body["status"] == "success" and body["stat_data"]
    assert set(body["misc_data"]) == {"dl_bandwidth", "ul_bandwidth"}


async def test_compute_requires_machine(serve):
    client = serve(root.make_compute_app())
    response = await client.post("/compute", data={"action": "FETCH_ESTIMATED_CHARTS"})
    assert response.status_code == 400
