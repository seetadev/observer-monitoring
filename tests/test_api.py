import json
import os

import pytest

import root
from tests.conftest import make_stat


async def api(client, **data):
    return await client.post("/api", data=data)


async def add_machine(db, name="box"):
    await db["machines"].insert_one({"name": name, "ip": "10.0.0.1", "last_online": "1", "machine": "m", "node": "n",
                                     "architecture": ["64bit", ""], "system": "Linux", "release": "r",
                                     "version": "v", "memory": 1})


@pytest.mark.parametrize("page", ["live", "stat", "perf", "cost", "admin"])
async def test_pages_render(dash, page):
    response = await dash.get("/" + page)
    assert response.status_code == 200
    assert "<html" in response.text.lower()


async def test_static_files_are_served(dash):
    assert (await dash.get("/css/styles.css")).status_code == 200


async def test_cors_header_is_present_on_api(dash):
    response = await api(dash, action="GET_REMOTE_MACHINES")
    assert response.headers["access-control-allow-origin"] == "*"


async def test_missing_action_is_400(dash):
    response = await dash.post("/api", data={})
    assert response.status_code == 400


async def test_unknown_action_is_400(dash):
    response = await api(dash, action="NOPE")
    assert response.status_code == 400 and "Not a valid action" in response.text


async def test_remote_machines_lists_registered_machines(dash, db):
    await add_machine(db)
    body = (await api(dash, action="GET_REMOTE_MACHINES")).json()
    machines = json.loads(body["remoteMachines"])["machines"]
    assert machines == [{"name": "box", "ip": "10.0.0.1", "last_online": "1"}]


async def test_get_specs(dash, db):
    await add_machine(db)
    body = (await api(dash, action="GET_SPECS", machine_name="box")).json()
    assert body["system"] == "Linux" and body["memory"] == 1


@pytest.mark.xfail(strict=True, reason="an unknown machine currently produces HTTP 500")
async def test_get_specs_unknown_machine_is_a_client_error(dash):
    assert (await api(dash, action="GET_SPECS", machine_name="ghost")).status_code == 404


async def test_instance_pricing(dash, db):
    await root.init_instance_pricing()
    body = (await api(dash, action="FETCH_INSTANCE_PRICING", provider_name="amazon-ec2")).json()
    instances = json.loads(body["response_data"])["instances"]
    assert {i["name"] for i in instances} >= {"t2.micro", "m4.large"}


async def test_save_list_and_fetch_a_test(dash):
    saved = (await api(dash, action="SAVE_TEST", machine="box", testName="t1", testCode="x = 1")).json()
    assert saved["status"] == "success"
    listed = (await api(dash, action="FETCH_TESTS_LIST", machine="box")).json()
    assert json.loads(listed["response_data"])["tests"] == [{"name": "t1", "machine": "box"}]
    fetched = (await api(dash, action="FETCH_TEST", machine="box", test_name="t1")).json()
    assert json.loads(fetched["response_data"])["script"] == "x = 1"


async def test_save_test_with_missing_argument_reports_failure(dash):
    body = (await api(dash, action="SAVE_TEST", machine="box")).json()
    assert body["status"] == "failure"


async def test_admin_login_checks_the_password(dash):
    assert (await api(dash, action="LOGIN_ADMIN", password=root.ADMIN_PASSWORD)).json() == {"auth": "True"}
    assert (await api(dash, action="LOGIN_ADMIN", password="definitely-wrong")).json() == {"auth": "False"}


async def test_delete_machine_removes_machine_and_collections(dash, db):
    await add_machine(db)
    await db["[box]-live"].insert_one(make_stat())
    await db["[box]-stat"].insert_one(make_stat())
    assert (await api(dash, action="DELETE_REMOTE_MACHINE", machine_id="box")).json() == {"status": "True"}
    assert await db["machines"].count_documents({}) == 0
    assert "[box]-live" not in await db.list_collection_names()
    assert "[box]-stat" not in await db.list_collection_names()


async def test_stat_data_is_condensed_for_the_chart(dash, db):
    await db["[box]-stat"].insert_many([make_stat(ram=float(i)) for i in range(3)])
    body = (await api(dash, action="GET_STAT_DATA", **{"client-name": "box"})).json()
    assert [r["ram"] for r in body["stat_data"]] == [0.0, 1.0]  # last window dropped (see xfail below)


@pytest.mark.xfail(strict=True, reason="a single stat record is returned as an empty list")
async def test_stat_data_with_one_record_is_not_empty(dash, db):
    await db["[box]-stat"].insert_one(make_stat())
    body = (await api(dash, action="GET_STAT_DATA", **{"client-name": "box"})).json()
    assert len(body["stat_data"]) == 1


async def test_stat_data_requires_client_name(dash):
    assert (await api(dash, action="GET_STAT_DATA")).status_code == 400


async def test_export_csv_and_xlsx(dash, db):
    data = json.dumps({"live_data": [make_stat(), make_stat()], "snaps_id": []})
    for file_type, suffix in (("CSV", ".csv"), ("XLSX", ".xlsx")):
        response = await api(dash, action="SAVE_TEST_RESULT_AS", jsonData=data, test_name="run", file_type=file_type)
        body = response.json()
        name = body["file_name"] + ("" if body["file_name"].endswith(suffix) else suffix)
        from config import FILES_DIR
        assert os.path.getsize(os.path.join(FILES_DIR, name)) > 0
