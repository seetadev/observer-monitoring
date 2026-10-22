"""Every dashboard chart renders with data, in a real browser (Playwright + Chromium)."""
import asyncio
import datetime
import math
import os
import pathlib

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright
from tornado.httpserver import HTTPServer
from tornado.testing import bind_unused_port
from tornado.web import create_signed_value

import auth
import config
import root
from tests.conftest import TEST_SECRET, make_stat

ROOT = pathlib.Path(__file__).resolve().parent.parent
MACHINE = "chart-box"
POINTS = 90


def chromium_ready():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not chromium_ready(), reason="Chromium for Playwright is not installed")


def full_app():
    """The dashboard with the live and compute services mounted on the same origin (as behind the TLS proxy)."""
    app = root.make_dash_app()
    for path, handler in (("/_live", root.LiveMonitorHandler), ("/_compute", root.ComputeHandler)):
        before = len(app.wildcard_router.rules)
        app.wildcard_router.add_rules([(path, handler)])
        app.wildcard_router.rules.insert(0, app.wildcard_router.rules.pop(before))   # before the static catch-all
    return app


@pytest_asyncio.fixture
async def site(db):
    await root.init_instance_pricing()
    await db["machines"].insert_one({"name": MACHINE, "ip": "10.0.0.1", "last_online": "1", "machine": "m", "node": "n",
                                     "architecture": ["64bit", ""], "system": "Linux", "release": "r", "version": "v",
                                     "memory": 8 * 1024 ** 3})
    now = datetime.datetime.now()
    records = []
    for i in range(POINTS):
        stamp = now - datetime.timedelta(minutes=POINTS - i)
        records.append(make_stat(
            name=MACHINE, date=stamp.isoformat(), cpu=[30 + 20 * math.sin(i / 7.0 + core) for core in range(4)],
            ram=40 + 10 * math.sin(i / 11.0), bytes_recv=1_000_000 * (i + 1), bytes_sent=500_000 * (i + 1),
            disk_total=100 * 1024 ** 3, disk_used=(20 + i / 10.0) * 1024 ** 3))
    await db["[%s]-stat" % MACHINE].insert_many(records)

    async def feed():
        n = 0
        while True:
            n += 1
            await db["[%s]-live" % MACHINE].insert_one(make_stat(
                name=MACHINE, cpu=[20 + (n * 7 + k * 13) % 60 for k in range(4)], ram=50 + n % 20,
                dl_rate=1000 * (n % 9 + 1), ul_rate=800 * (n % 7 + 1), disk_read_rate=2048 * (n % 5 + 1),
                disk_write_rate=4096 * (n % 6 + 1), swap=10.0))
            await asyncio.sleep(0.25)

    feeder = asyncio.create_task(feed())
    sock, port = bind_unused_port()
    server = HTTPServer(full_app())
    server.add_sockets([sock])
    yield "http://127.0.0.1:%d" % port
    feeder.cancel()
    server.stop()


@pytest_asyncio.fixture
async def page(site):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        session = create_signed_value(TEST_SECRET, auth.SESSION_COOKIE, "admin").decode()
        await context.add_cookies([
            {"name": auth.SESSION_COOKIE, "value": session, "url": site},
            {"name": "machine-name", "value": MACHINE, "url": site}])
        page = await context.new_page()
        page.problems = []
        page.on("pageerror", lambda e: page.problems.append("pageerror: " + str(e)))
        page.on("console", lambda m: page.problems.append("console: " + m.text)
                if m.type == "error" and "favicon" not in m.text else None)
        page.requested = []
        page.on("request", lambda r: page.requested.append(r.url))
        yield page
        await context.close()
        await browser.close()


# What the browser reports about a chart canvas: the Chart.js data and how much of it was actually painted.
INSPECT = """(id) => {
  const box = document.getElementById(id);
  const canvases = box ? box.querySelectorAll('canvas') : [];
  if (canvases.length !== 1) return {canvases: canvases.length};
  const canvas = canvases[0];
  const chart = Chart.getChart(canvas);
  const ctx = canvas.getContext('2d');
  const px = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  let painted = 0;
  for (let i = 0; i < px.length; i += 4) {
    const near = (a, b) => Math.abs(a - b) < 8;
    if (!(near(px[i], 51) && near(px[i + 1], 51) && near(px[i + 2], 51))) painted++;
  }
  const corner = Array.from(ctx.getImageData(2, 2, 1, 1).data);
  return {canvases: 1, hasChart: !!chart, type: chart && chart.config.type,
          datasets: chart ? chart.data.datasets.map(d => d.data.length) : [],
          painted: painted, size: [canvas.width, canvas.height], corner: corner};
}"""

CHARTS = {"cpu_chart": (4, 3000), "ram_chart": (1, 800), "net_chart": (2, 800), "storage_chart": (1, 800)}


async def inspect(page, chart_id):
    return await page.evaluate(INSPECT, chart_id)


async def wait_for_charts(page, ids=tuple(CHARTS), timeout=20):
    for _ in range(int(timeout / 0.25)):
        states = [await inspect(page, i) for i in ids]
        if all(s.get("hasChart") and s["datasets"] and all(s["datasets"]) for s in states):
            return states
        await asyncio.sleep(0.25)
    return states


def assert_rendered(states, ids=tuple(CHARTS)):
    for chart_id, state in zip(ids, states):
        datasets, painted = CHARTS[chart_id]
        assert state["canvases"] == 1, (chart_id, state)                       # redraws never stack canvases
        assert state["hasChart"] and state["type"] == "line", (chart_id, state)
        assert len(state["datasets"]) == datasets, (chart_id, state)           # one line per core / direction
        assert min(state["datasets"]) >= 10, (chart_id, state)                 # real data, not an empty chart
        assert state["painted"] > painted, (chart_id, state)                   # lines actually drawn
        assert state["corner"] == [51, 51, 51, 255], (chart_id, state)         # the dark #333333 background


async def test_statistics_page_draws_all_four_charts_from_the_stored_data(page, site):
    await page.goto(site + "/stat")
    assert_rendered(await wait_for_charts(page))
    assert not page.problems, page.problems
    if os.environ.get("E2E_SHOT_DIR"):
        await page.screenshot(path=os.path.join(os.environ["E2E_SHOT_DIR"], "stat-charts.png"), full_page=True)


async def test_statistics_page_toggle_redraws_without_stacking_charts(page, site):
    await page.goto(site + "/stat")
    await wait_for_charts(page)
    await page.get_by_text("Toggle Disjoint Areas").click()
    await asyncio.sleep(0.5)
    assert_rendered(await wait_for_charts(page))
    await page.get_by_text("Toggle Disjoint Areas").click()
    await asyncio.sleep(0.5)
    assert_rendered(await wait_for_charts(page))
    assert not page.problems, page.problems


async def test_cost_page_draws_its_demand_curves(page, site):
    await page.goto(site + "/cost")
    states = await wait_for_charts(page)          # the storage curve sits in a hidden row but is drawn as well
    assert_rendered(states)
    assert not page.problems, page.problems


async def test_cost_page_toggle_redraws(page, site):
    await page.goto(site + "/cost")
    await wait_for_charts(page)
    await page.get_by_text("Toggle Disjoint Areas").click()
    await asyncio.sleep(0.5)
    assert_rendered(await wait_for_charts(page))


LIVE_CANVASES = ("graph-cpu", "graph-ram", "graph-disk", "graph-network")

LIVE_PAINTED = """(id) => {
  const c = document.getElementById(id);
  if (!c) return -1;
  const px = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  let n = 0;
  for (let i = 3; i < px.length; i += 4) if (px[i] > 0) n++;
  return n;
}"""


async def test_live_page_streams_all_four_charts(page, site):
    await page.goto(site + "/live")
    painted = {}
    for _ in range(60):
        painted = {i: await page.evaluate(LIVE_PAINTED, i) for i in LIVE_CANVASES}
        if all(v > 300 for v in painted.values()):
            break
        await asyncio.sleep(0.5)
    assert all(v > 300 for v in painted.values()), painted
    assert not page.problems, page.problems


async def test_gaps_in_the_data_are_drawn_as_gaps(page, site):
    await page.goto(site + "/stat")
    await wait_for_charts(page)
    result = await page.evaluate('''() => {
      const box = document.createElement('div'); box.id = 'gap_test'; document.body.appendChild(box);
      const chart = renderLineChart('gap_test', [{values: [[1000, 1], [2000, 2], null, null, [9000, 3], [10000, 4]],
                                                  color: '#ff0000'}], 200, 400);
      const data = chart.data.datasets[0].data;
      return {points: data.length, nulls: data.filter(p => p.y === null).length,
              xs: data.map(p => p.x), spanGaps: chart.data.datasets[0].spanGaps};
    }''')
    assert result["nulls"] == 2 and result["spanGaps"] is False and result["points"] == 6
    assert result["xs"] == sorted(result["xs"])                  # still ordered on the time axis
    assert not page.problems, page.problems


async def test_no_zingchart_is_loaded_anywhere(page, site):
    for path in ("/stat", "/cost", "/live", "/perf", "/status"):
        await page.goto(site + path)
        await asyncio.sleep(0.3)
        assert await page.evaluate("typeof zingchart") == "undefined", path
    assert not [u for u in page.requested if "zingchart" in u.lower()]


# --- licence and files (no browser needed) -------------------------------------------------------------------------

def test_zingchart_and_the_old_chart_js_are_gone_and_chart_js_is_mit():
    js = ROOT / "static" / "js"
    assert not (js / "zingchart.min.js").exists() and not (js / "Chart.js").exists()
    assert not (js / "Chart.HorizontalBar.js").exists()
    header = (js / "chart.umd.min.js").read_text()[:300]
    assert "Chart.js v4" in header and "Released under the MIT License" in header
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    assert "Chart.js" in notices and "MIT" in notices and "ZingChart" in notices


def test_no_source_file_mentions_zingchart_functions():
    for path in list((ROOT / "static" / "js").glob("*.js")) + list((ROOT / "templates").glob("*.html")):
        if path.name == "chart.umd.min.js":
            continue
        assert "zingchart" not in path.read_text().lower(), path.name


def test_pages_load_chart_js_and_the_helper_before_their_scripts():
    for page, script in (("stat", "stat.js"), ("cost", "cost.js")):
        html = (ROOT / "templates" / (page + ".html")).read_text()
        assert html.index("chart.umd.min.js") < html.index("charts.js") < html.index(script)


def test_config_is_unchanged_by_the_chart_swap():
    assert config.LOADTEST_MAX_CONCURRENCY >= 1
