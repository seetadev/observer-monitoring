import asyncio
import csv
import io
import json
import re
import zipfile

import pytest

import auth
import config
import root
import validation
from tests.conftest import make_stat


def export_data(rows=None):
    return json.dumps({"live_data": rows if rows is not None else [make_stat(), make_stat()]})


async def export(client, test_name="run", file_type="CSV", data=None):
    return await client.post("/api", data={"action": "SAVE_TEST_RESULT_AS", "jsonData": data or export_data(),
                                           "test_name": test_name, "file_type": file_type})


# --- export file names stay inside the export directory ---------------------------------------------------------

@pytest.mark.parametrize("name", ["../escaped", "..\\escaped", "/tmp/escaped", "a/b", "a\x00b", "", "a" * 65,
                                  "with space", "..", "."])
@pytest.mark.parametrize("file_type", ["CSV", "XLSX"])
async def test_unsafe_export_names_are_refused_and_write_nothing(dash, tmp_path, name, file_type):
    response = await export(dash, test_name=name, file_type=file_type)
    assert response.status_code == 400
    assert not list(tmp_path.rglob("escaped*"))
    assert not list((tmp_path / "files_buffer").glob("*")) if (tmp_path / "files_buffer").exists() else True


async def test_unknown_export_type_is_refused(dash):
    assert (await export(dash, file_type="EXE")).status_code == 400


@pytest.mark.parametrize("file_type,suffix", [("CSV", ".csv"), ("XLSX", ".xlsx")])
async def test_export_is_written_inside_the_export_directory_with_a_server_chosen_name(dash, tmp_path, file_type,
                                                                                         suffix):
    body = (await export(dash, test_name="run-1", file_type=file_type)).json()
    assert re.fullmatch(r"run-1_\d{8}T\d{6}_[0-9a-f]{8}" + re.escape(suffix), body["file_name"])
    written = list((tmp_path / "files_buffer").iterdir())
    assert [p.name for p in written] == [body["file_name"]]
    assert written[0].stat().st_size > 0


async def test_two_exports_never_share_a_file(dash, tmp_path):
    first = (await export(dash)).json()["file_name"]
    second = (await export(dash)).json()["file_name"]
    assert first != second


@pytest.mark.parametrize("payload", ["not json", json.dumps({}), json.dumps({"live_data": []}),
                                     json.dumps({"live_data": "x"}), json.dumps({"live_data": [1, 2]}),
                                     json.dumps([1])])
async def test_malformed_export_data_is_a_client_error(dash, payload):
    assert (await export(dash, data=payload)).status_code == 400


async def test_export_no_longer_reads_arbitrary_files_for_images(dash, tmp_path):
    marker = tmp_path / "private.png"
    marker.write_bytes(b"\x89PNG\r\n\x1a\n" + b"secret-image-bytes")
    payload = json.dumps({"live_data": [make_stat()], "snaps_id": [str(marker)]})
    body = (await export(dash, file_type="XLSX", data=payload)).json()
    exported = tmp_path / "files_buffer" / body["file_name"]
    with zipfile.ZipFile(exported) as archive:
        assert not [n for n in archive.namelist() if n.startswith("xl/media")]
    assert not hasattr(root, "Image")


# --- spreadsheet formulas stay text -----------------------------------------------------------------------------

FORMULA_ROW = {"a": "=1+1", "b": "+SUM(A1)", "c": "-2+3", "d": "@x", "e": "plain", "f": 5, "g": -5}


async def test_csv_export_neutralises_formulas_and_keeps_numbers(dash, tmp_path):
    name = (await export(dash, data=export_data([FORMULA_ROW]))).json()["file_name"]
    rows = list(csv.reader(io.StringIO((tmp_path / "files_buffer" / name).read_text())))
    assert rows[1] == ["'=1+1", "'+SUM(A1)", "'-2+3", "'@x", "plain", "5", "-5"]


async def test_xlsx_export_contains_no_formulas(dash, tmp_path):
    name = (await export(dash, file_type="XLSX", data=export_data([FORMULA_ROW]))).json()["file_name"]
    with zipfile.ZipFile(tmp_path / "files_buffer" / name) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        strings = archive.read("xl/sharedStrings.xml").decode()
    assert "<f>" not in sheet
    assert "&apos;=1+1" in strings or "'=1+1" in strings


async def test_columns_follow_the_first_record(dash, tmp_path):
    rows = [{"x": 1, "y": 2}, {"y": 20, "x": 10}]
    name = (await export(dash, data=export_data(rows))).json()["file_name"]
    parsed = list(csv.reader(io.StringIO((tmp_path / "files_buffer" / name).read_text())))
    assert parsed == [["x", "y"], ["1", "2"], ["10", "20"]]


# --- downloads --------------------------------------------------------------------------------------------------

async def test_downloads_are_attachments_and_not_sniffed(dash):
    name = (await export(dash)).json()["file_name"]
    response = await dash.get("/files/" + name)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == "attachment"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("url", ["/files/..%2f..%2fconfig.py", "/files/../config.py", "/files/%2e%2e/config.py",
                                 "/files/..%5cconfig.py"])
async def test_downloads_cannot_leave_the_export_directory(dash, url):
    response = await dash.get(url)
    assert response.status_code in (400, 403, 404)
    assert "COOKIE_SECRET" not in response.text


# --- machine names ----------------------------------------------------------------------------------------------

BAD_NAMES = ["a]-live", "../x", "<img src=x onerror=alert(1)>", "x" * 65, "a b", "a\"b", "x;y", "a/b", "é"]


@pytest.mark.parametrize("name", BAD_NAMES)
async def test_dashboard_api_rejects_bad_machine_names(dash, db, name):
    for action, field in (("GET_SPECS", "machine_name"), ("GET_STAT_DATA", "client-name"),
                          ("DELETE_REMOTE_MACHINE", "machine_id")):
        response = await dash.post("/api", data={"action": action, field: name})
        assert response.status_code == 400, action
    assert [c for c in await db.list_collection_names() if "]-" in c] == []


@pytest.mark.parametrize("name", BAD_NAMES)
async def test_live_and_compute_reject_bad_machine_names(serve, db, name):
    live, compute = serve(root.make_live_app()), serve(root.make_compute_app())
    assert (await live.post("/live", data={"action": "GET_LIVE_DATA", "client-name": name})).status_code == 400
    for action in ("GET_COST_STATS", "FETCH_ESTIMATED_CHARTS"):
        response = await compute.post("/compute", data={"action": action, "machine": name, "days_duration": "1"})
        assert response.status_code == 400


@pytest.mark.parametrize("name", ["box", "web-01", "db_2", "host.example.org", "A1"])
def test_ordinary_machine_names_are_accepted(name):
    assert validation.valid_name(name)


# --- agent records ----------------------------------------------------------------------------------------------

def test_clean_stats_keeps_only_known_numeric_fields():
    record = dict(make_stat(), evil="<script>", **{"$where": "1"})
    cleaned = validation.clean_record(record)
    assert "evil" not in cleaned and "$where" not in cleaned
    assert cleaned["cpu"] == [10.0, 20.0] and cleaned["ram"] == 50.0


@pytest.mark.parametrize("override", [
    {"ram": "<img src=x onerror=1>"}, {"ram": True}, {"ram": None}, {"cpu": ["<b>"]}, {"cpu": []},
    {"cpu": "10"}, {"cpu": [1] * 2000}, {"packets_sent": [1]}, {"packets_recv": ["a", "b"]}, {"date": "x" * 500},
    {"date": 5}, {"name": "<b>x</b>"}, {"name": 5}, {"uptime": float("inf")}, {"bytes_sent": float("nan")},
])
def test_clean_stats_rejects_non_numeric_or_oversized_values(override):
    with pytest.raises(ValueError):
        validation.clean_record(dict(make_stat(), **override))


def test_clean_stats_requires_every_field():
    record = make_stat()
    del record["disk_used"]
    with pytest.raises(ValueError):
        validation.clean_record(record)


SPECS = {"machine": "m", "node": "n", "architecture": ["64bit", ""], "system": "Linux", "release": "r",
         "version": "v", "name": "box", "memory": 123}


@pytest.mark.parametrize("override", [{"node": "x" * 500}, {"machine": 5}, {"architecture": "64"},
                                      {"architecture": ["a", "b", "c"]}, {"memory": "1GB"}, {"name": "a b"}])
def test_clean_specs_rejects_bad_values(override):
    with pytest.raises(ValueError):
        validation.clean_record(dict(SPECS, **override))


def test_clean_specs_accepts_the_agent_specs():
    assert validation.clean_record(SPECS)["memory"] == 123


@pytest.fixture
async def listener(db):
    from tornado.testing import bind_unused_port
    sock, port = bind_unused_port()
    server = root.TCPListener()
    server.add_sockets([sock])
    yield port
    server.stop()


async def _talk(port, token, lines):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = [json.dumps({"token": token}) + "\n"] + [
        (line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines]
    writer.write("".join(payload).encode())
    await writer.drain()
    await asyncio.sleep(0.4)
    try:
        closed = await asyncio.wait_for(reader.read(1), 1) == b""
    except asyncio.TimeoutError:
        closed = False
    writer.close()
    return closed


async def test_listener_stores_only_cleaned_records(db, listener):
    token = await auth.create_agent("box")
    await _talk(listener, token, [SPECS, dict(make_stat(), evil="x")])
    stored = await db["[box]-live"].find_one()
    assert stored is not None and "evil" not in stored


@pytest.mark.parametrize("bad", [dict(make_stat(), ram="<script>alert(1)</script>"),
                                 '{"name": "box", "ram": NaN}', dict(make_stat(), cpu=[{"$gt": 1}])])
async def test_listener_drops_the_connection_on_invalid_metrics(db, listener, bad):
    token = await auth.create_agent("box")
    assert await _talk(listener, token, [bad]) is True
    assert "[box]-live" not in await db.list_collection_names()


# --- response headers -------------------------------------------------------------------------------------------

async def test_pages_and_api_set_hardening_headers(dash):
    for response in (await dash.get("/live"), await dash.post("/api", data={"action": "GET_REMOTE_MACHINES"})):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "same-origin"


# --- front end: server data is inserted as text, not markup -----------------------------------------------------

def _js(name):
    return open(config.STATIC_DIR + "/js/" + name).read()


def test_sidebar_and_admin_do_not_build_markup_from_machine_data():
    shared, admin, cost = _js("shared-scripts.js"), _js("admin.js"), _js("cost.js")
    assert "changeMachine(\\\"" not in shared and "onclick='changeMachine" not in shared
    assert '"+name+"' not in shared and '"+ip+"' not in shared
    assert "onclick=\\\"deleteMachine" not in admin and "attr(\"id\")" not in admin
    assert "onclick=\\\"setCurrentInstanceType" not in cost
