import asyncio
import calendar
import csv
import datetime
import json
import logging
import os
import time
import uuid

import dateutil.parser
import tornado.iostream
import tornado.web
import xlsxwriter
from tornado.httpserver import HTTPServer
from tornado.iostream import StreamClosedError
from tornado.tcpserver import TCPServer
from tornado.web import Application

import auth
import config
import db
import monitor
import uimodules
import validation
from db import ensure_machine_indexes, forget_machine, get_db, live_name, stat_name

__author__ = 'sdpatro'

log = logging.getLogger('observer')

MAX_LINE_BYTES = 64 * 1024
MAX_EXPORT_ROWS = 100000
AUTH_TIMEOUT_SECONDS = 10
AGENT_RECHECK_SECONDS = 30


# Scalar and vector metrics that are averaged when records are condensed.
SCALAR_FIELDS = ('ram', 'disk_io_read', 'disk_io_write', 'disk_read_rate', 'disk_write_rate', 'bytes_recv',
                 'bytes_sent', 'ul_rate', 'dl_rate', 'disk_total', 'disk_used')
VECTOR_FIELDS = ('cpu', 'packets_sent', 'packets_recv')

EC2_INSTANCES = [
    {'name': 't1.micro', 'vCpu': 1, 'memory': 0.6, 'io': 1, 'hourly-cost': 0.02},
    {'name': 't2.nano', 'vCpu': 1, 'memory': 0.5, 'io': 2, 'hourly-cost': 0.007},
    {'name': 't2.micro', 'vCpu': 1, 'memory': 1.0, 'io': 2.5, 'hourly-cost': 0.013},
    {'name': 't2.small', 'vCpu': 1, 'memory': 2.0, 'io': 2.5, 'hourly-cost': 0.026},
    {'name': 't2.medium', 'vCpu': 2, 'memory': 4.0, 'io': 2.5, 'hourly-cost': 0.052},
    {'name': 't2.large', 'vCpu': 2, 'memory': 8.0, 'io': 2.5, 'hourly-cost': 0.104},
    {'name': 'm4.large', 'vCpu': 2, 'memory': 8.0, 'io': 3, 'hourly-cost': 0.120},
    {'name': 'm4.xlarge', 'vCpu': 4, 'memory': 16.0, 'io': 4, 'hourly-cost': 0.239},
    {'name': 'm4.2xlarge', 'vCpu': 8, 'memory': 32.0, 'io': 4, 'hourly-cost': 0.479},
]


async def init_instance_pricing():
    """Seed the EC2 price table. Idempotent, so several processes can start at once."""
    await get_db()['cloud-pricing'].update_one(
        {'name': 'amazon-ec2'},
        {'$setOnInsert': {'name': 'amazon-ec2', 'instances': EC2_INSTANCES}},
        upsert=True)


def average_group(group):
    """Average a list of stat/live records field by field."""
    count = float(len(group))
    result = {}
    for field in SCALAR_FIELDS:
        result[field] = float(sum(record[field] for record in group) / count)
    for field in VECTOR_FIELDS:
        width = len(group[0][field])
        result[field] = [float(sum(record[field][i] for record in group) / count) for i in range(width)]
    return result


# The TCP listener endpoint for all remote senders.
def _reject_constant(name):
    raise ValueError("non-finite number in record")


class TCPListener(TCPServer):
    """Agents must send {"token": ...} first; unknown or wrong tokens are disconnected without being stored."""

    async def handle_stream(self, stream, address):
        ip = address[0]
        db = get_db()
        try:
            agent = await self.authenticate(stream)
            if agent is None:
                log.warning("Rejected agent connection from %s", ip)
                return
            print("New agent connected: " + agent + " (" + ip + ")", flush=True)
            checked = time.monotonic()
            while True:
                data = await stream.read_until(b'\n', max_bytes=MAX_LINE_BYTES)
                if time.monotonic() - checked >= AGENT_RECHECK_SECONDS:
                    if not await auth.agent_exists(agent):
                        log.warning("Agent %s was revoked; closing connection", agent)
                        return
                    checked = time.monotonic()
                await self.store_record(data, db, ip, agent)
        except StreamClosedError:
            return
        except (ValueError, KeyError, TypeError, asyncio.TimeoutError, tornado.iostream.UnsatisfiableReadError) as exc:
            log.warning("Closing agent connection from %s: %s", ip, exc.__class__.__name__)
        finally:
            stream.close()

    async def authenticate(self, stream):
        line = await asyncio.wait_for(stream.read_until(b'\n', max_bytes=MAX_LINE_BYTES), AUTH_TIMEOUT_SECONDS)
        try:
            message = json.loads(line)
        except ValueError:
            return None
        if not isinstance(message, dict):
            return None
        agent = await auth.find_agent(message.get('token'))
        return agent['name'] if agent else None

    async def store_record(self, data, db, ip, agent):
        data_json = validation.clean_record(json.loads(data, parse_constant=_reject_constant))
        if data_json['name'] != agent:
            raise ValueError("record does not belong to the authenticated agent")

        if 'node' in data_json:
            await update_machine(data_json, ip)

        else:
            live_coll = db[live_name(data_json['name'])]
            stat_coll = db[stat_name(data_json['name'])]

            await update_machine_time(data_json["name"])

            await ensure_machine_indexes(data_json['name'])
            data_json['ts'] = datetime.datetime.now(datetime.timezone.utc)
            await live_coll.insert_one(data_json)
            await condense(stat_coll, live_coll)


async def update_machine(json_data, machine_ip):
    machines = get_db()["machines"]
    machine_name = json_data["name"]
    machine = await machines.find_one({'name': machine_name})
    if machine is None:
        await machines.insert_one(
            {'name': machine_name, 'ip': machine_ip, 'last_online': str(calendar.timegm(time.gmtime())),
             'machine': json_data['machine'], 'node': json_data['node'], 'architecture': json_data['architecture'],
             'system': json_data['system'], 'release': json_data['release'], 'version': json_data['release'],
             'memory': json_data['memory'], 'interval': json_data.get('interval')})
    else:
        await machines.update_one({'name': machine_name},
                                  {'$set': {'ip': machine_ip, 'interval': json_data.get('interval')}})


async def update_machine_time(machine_name):
    await get_db()["machines"].update_one(
        {'name': machine_name}, {'$set': {'last_online': str(calendar.timegm(time.gmtime()))}})


# Live 60 secs monitoring data is converted to statistical 1-minute gradient limitless data.
async def condense(stat_coll, live_coll):
    while await live_coll.count_documents({}) > 120:
        live_data = await live_coll.find().sort('_id', 1).limit(60).to_list(60)

        stat = average_group(live_data)
        stat['date'] = str(datetime.datetime.now().isoformat())
        stat['name'] = live_data[-1]['name']
        stat['ts'] = datetime.datetime.now(datetime.timezone.utc)

        await live_coll.delete_many({'_id': {'$in': [record['_id'] for record in live_data]}})
        print("Condensing...", flush=True)
        await stat_coll.insert_one(stat)


async def _serve_tcp(server, port, label):
    auth.validate_config()
    await init_instance_pricing()
    await db.ensure_indexes()
    server.listen(int(port), address=config.BIND_ADDRESS)
    print(label + " running at port " + str(port), flush=True)
    await asyncio.Event().wait()


# Fire up the TCP server.
def start_listener(port):
    asyncio.run(_serve_tcp(TCPListener(), port, "Observer TCP"))


########################################################################


EXPORT_SUFFIX = {"CSV": ".csv", "XLSX": ".xlsx"}


def export_path(file_name):
    """Absolute path of an export file; refuses anything that would leave FILES_DIR."""
    base = os.path.realpath(config.FILES_DIR)
    path = os.path.realpath(os.path.join(base, file_name))
    if os.path.dirname(path) != base:
        raise ValueError("export path escapes the export directory")
    os.makedirs(base, exist_ok=True)
    return path


def spreadsheet_safe(value):
    """Text that a spreadsheet application would read as a formula is prefixed so it stays text."""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def write_csv(path, columns, rows):
    with open(path, "x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([spreadsheet_safe(column) for column in columns])
        for row in rows:
            writer.writerow([spreadsheet_safe(value) for value in row])


def write_xlsx(path, columns, rows):
    workbook = xlsxwriter.Workbook(path, {"strings_to_formulas": False, "strings_to_urls": False})
    worksheet = workbook.add_worksheet()
    bold_header = workbook.add_format({'bold': True, 'font_color': '#0b9f51'})
    large_bold_green = workbook.add_format({'bold': True, 'font_size': 18, 'font_color': '#009245'})
    worksheet.set_row(0, 30)
    worksheet.write_string(0, 0, "Stats", large_bold_green)
    for col, column in enumerate(columns):
        worksheet.write_string(2, col, spreadsheet_safe(column), bold_header)
        worksheet.set_column(col, col, 20)
    for row_index, row in enumerate(rows):
        for col, value in enumerate(row):
            value = spreadsheet_safe(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                worksheet.write_number(3 + row_index, col, value)
            else:
                worksheet.write_string(3 + row_index, col, str(value))
    workbook.close()


class ApiHandler(auth.ApiBase):
    def requires_auth(self):
        return self.get_argument("action", None) not in ("LOGIN_ADMIN", "LOGOUT_ADMIN")

    async def login(self):
        password = self.get_argument("password", "")
        key = auth.client_address(self.request)
        if auth.login_limiter.blocked(key):
            self.set_header("Retry-After", str(auth.login_limiter.window))
            self.finish_login(429, "Too many failed attempts. Try again later.")
            return
        stored = config.ADMIN_PASSWORD_HASH
        ok = bool(stored) and await asyncio.to_thread(auth.verify_password, password, stored)
        if not ok:
            auth.login_limiter.record_failure(key)
            self.finish_login(401, "Invalid credentials.")
            return
        auth.login_limiter.reset(key)
        self.start_session()
        self.finish(dict(auth="True"))

    def finish_login(self, status, message):
        self.set_status(status)
        self.finish(dict(auth="False", message=message))

    async def export_results(self):
        """Write live_data to FILES_DIR as CSV or XLSX under a server-chosen file name."""
        test_name = self.get_argument("test_name", "")
        file_type = self.get_argument("file_type", "")
        if not validation.valid_name(test_name) or file_type not in EXPORT_SUFFIX:
            self.error_respond(400, "Invalid export request.")
            return
        try:
            live_data = json.loads(self.get_argument("jsonData"))["live_data"]
        except (ValueError, KeyError, TypeError):
            self.error_respond(400, "Invalid export data.")
            return
        if (not isinstance(live_data, list) or not 0 < len(live_data) <= MAX_EXPORT_ROWS
                or not all(isinstance(record, dict) for record in live_data)):
            self.error_respond(400, "Invalid export data.")
            return

        columns = [str(column) for column in live_data[0]]
        rows = [[record.get(column, "") for column in live_data[0]] for record in live_data]
        file_name = "%s_%s_%s%s" % (test_name, datetime.datetime.now().strftime("%Y%m%dT%H%M%S"),
                                    uuid.uuid4().hex[:8], EXPORT_SUFFIX[file_type])
        path = export_path(file_name)
        await asyncio.to_thread(write_csv if file_type == "CSV" else write_xlsx, path, columns, rows)
        self.finish(dict(file_name=file_name))

    async def post(self):
        if self.get_argument("action", None) is None:
            self.error_respond(400, "No action specified")
            return

        db = get_db()
        action = self.get_argument("action")
        if action == "FETCH_INSTANCE_PRICING":
            provider_name = self.get_argument("provider_name", None)
            if provider_name is None:
                self.error_respond(400, "Provider name not provided.")
            else:
                instance_pricing = await db["cloud-pricing"].find_one({'name': provider_name})
                if instance_pricing is None:
                    self.error_respond(404, "Unknown provider.")
                    return
                instance_pricing['_id'] = str(instance_pricing['_id'])
                self.finish(dict(response_data=json.dumps(instance_pricing)))

        elif action == "GET_SPECS":
            machine_name = self.get_argument("machine_name", None)
            if machine_name is None:
                self.error_respond(400, "Machine name not provided.")
            elif not validation.valid_name(machine_name):
                self.error_respond(400, "Invalid machine name.")
            else:
                machine = await db["machines"].find_one({'name': machine_name})
                if machine is None:
                    self.error_respond(404, "Unknown machine.")
                    return
                self.finish({'machine': machine['machine'], 'node': machine['node'], 'version': machine['version'],
                             'release': machine['release'], 'system': machine['system'],
                             'architecture': machine['architecture'], 'memory': machine['memory']})

        elif action == "GET_STAT_DATA":
            client_name = self.machine_name("client-name")
            stat_data = []
            async for record in db[stat_name(client_name)].find().sort('_id', 1):
                record['_id'] = str(record['_id'])
                stat_data.append(record)

            if len(stat_data) > 1000:
                gradient = int(len(stat_data) / 1000)
            else:
                gradient = 1

            stat_data = condense_data_estimation(stat_data, gradient)
            self.finish(dict(stat_data=stat_data))

        elif action == "GET_REMOTE_MACHINES":
            remote_machines_list = []
            states = {doc['name']: doc async for doc in db["states"].find({"kind": "agent"})}
            async for remote_machine in db["machines"].find():
                state = states.get(remote_machine['name'], {})
                remote_machines_list.append({'name': remote_machine['name'], 'ip': remote_machine['ip'],
                                             'last_online': remote_machine['last_online'],
                                             'state': state.get('state', 'unknown'),
                                             'state_since': state.get('since')})
            self.finish((dict(remoteMachines=json.dumps({'machines': remote_machines_list}))))

        elif action == "GET_STATES":
            self.finish(await monitor.read_states())

        elif action == "GET_EVENTS":
            self.finish(dict(events=await monitor.read_events()))

        elif action == "LOGIN_ADMIN":
            await self.login()

        elif action == "LOGOUT_ADMIN":
            self.end_session()
            self.finish(dict(auth="False"))

        elif action == "DELETE_REMOTE_MACHINE":
            machine_name = self.machine_name("machine_id")
            try:
                await db[live_name(machine_name)].drop()
                await db[stat_name(machine_name)].drop()
                await db["machines"].delete_many({"name": machine_name})
                await db["states"].delete_one({"_id": "agent:" + machine_name})
                forget_machine(machine_name)
                self.finish(dict(status="True"))
            except Exception as e:
                self.error_respond(500, "Something went wrong: " + str(e))

        elif action == "SAVE_TEST_RESULT_AS":
            await self.export_results()

        else:
            self.error_respond(400, "Not a valid action")


class LiveHandler(auth.PageHandler):
    @tornado.web.authenticated
    def get(self):
        self.render("templates/live.html")


class StatHandler(auth.PageHandler):
    @tornado.web.authenticated
    def get(self):
        self.render("templates/stat.html")


class PerfHandler(auth.PageHandler):
    @tornado.web.authenticated
    def get(self):
        self.render("templates/perf.html")


class CostHandler(auth.PageHandler):
    @tornado.web.authenticated
    def get(self):
        self.render("templates/cost.html")


class AdminHandler(auth.PageHandler):
    @tornado.web.authenticated
    def get(self):
        self.render("templates/admin.html")


class StatusHandler(auth.PageHandler):
    @tornado.web.authenticated
    def get(self):
        self.render("templates/status.html")


class LoginHandler(auth.PageHandler):
    def get(self):
        self.render("templates/login.html", next_url=auth.safe_next(self.get_argument("next", None)))


def _app_settings(**extra):
    return auth.app_settings(**extra)


def make_dash_app():
    os.makedirs(config.FILES_DIR, exist_ok=True)
    return Application([(r"/", tornado.web.RedirectHandler, {"url": "/live"}),
                        (r"/api", ApiHandler),
                        (r"/login", LoginHandler),
                        (r"/live", LiveHandler),
                        (r"/stat", StatHandler),
                        (r"/perf", PerfHandler),
                        (r"/cost", CostHandler),
                        (r"/status", StatusHandler),
                        (r"/admin", AdminHandler),
                        (r"/files/(.*)", auth.AuthenticatedStaticFileHandler, {"path": config.FILES_DIR}),
                        (r"/fonts/(.*)", tornado.web.StaticFileHandler, {"path": config.FONTS_DIR}),
                        (r"/(.*)", tornado.web.StaticFileHandler, {"path": config.STATIC_DIR})],
                       **_app_settings(ui_modules=uimodules))


async def _serve_http(app, port, label):
    auth.validate_config()
    await init_instance_pricing()
    await db.ensure_indexes()
    server = HTTPServer(app)
    server.listen(int(port), address=config.BIND_ADDRESS)
    print(label + " running at port " + str(port), flush=True)
    await asyncio.Event().wait()


def start_dash_server(port):
    asyncio.run(_serve_http(make_dash_app(), port, "Observer Dashboard"))


#####################################################

class LiveMonitorHandler(auth.ApiBase):
    async def get_live_data(self, client_name):
        record = await get_db()[live_name(client_name)].find_one(sort=[("_id", -1)])
        if record is not None:
            record['_id'] = str(record['_id'])
            self.finish(record)

    async def post(self):
        action = self.get_argument("action", None)
        if action is not None:
            if action == "GET_LIVE_DATA":
                client_name = self.machine_name("client-name")
                await self.get_live_data(client_name)

        else:
            self.error_respond(400, "No action specified")


def make_live_app():
    return Application([(r"/live", LiveMonitorHandler)], **_app_settings())


def start_live_server(port):
    asyncio.run(_serve_http(make_live_app(), port, "Observer Live"))


#####################################################

def get_delta_time_days(stat_records):
    start_time = dateutil.parser.parse(stat_records[0]['date'])
    end_time = dateutil.parser.parse(stat_records[len(stat_records) - 1]['date'])
    delta_time_days = (end_time - start_time).days
    return delta_time_days


def get_delta_time_seconds(stat_records):
    start_time = dateutil.parser.parse(stat_records[0]['date'])
    end_time = dateutil.parser.parse(stat_records[len(stat_records) - 1]['date'])
    delta_time_seconds = (end_time - start_time).seconds
    return delta_time_seconds


async def get_uptime_percentage(machine):
    stat_coll = get_db()[stat_name(machine)]
    stat_count = await stat_coll.count_documents({})

    newest_stat_record = await stat_coll.find().sort("date", -1).limit(1).to_list(1)
    oldest_stat_record = await stat_coll.find().sort("date", 1).limit(1).to_list(1)
    if not newest_stat_record:
        return 0, 0

    old_timestamp = dateutil.parser.parse(oldest_stat_record[0]["date"])
    new_timestamp = dateutil.parser.parse(newest_stat_record[0]["date"])
    minutes_count = (new_timestamp - old_timestamp).seconds / 60
    minutes_count += (new_timestamp - old_timestamp).days * (24 * 60)
    return stat_count, minutes_count


def get_bandwidth_usage(records):
    dl_bytes = 0
    ul_bytes = 0
    for i in range(0, len(records)):
        if records[i - 1]['bytes_recv'] > records[i]['bytes_recv'] and i > 0:
            dl_bytes += records[i - 1]['bytes_recv']
        if records[i - 1]['bytes_sent'] > records[i]['bytes_sent'] and i > 0:
            ul_bytes += records[i - 1]['bytes_sent']

    dl_bytes += records[len(records) - 1]['bytes_recv']
    ul_bytes += records[len(records) - 1]['bytes_sent']
    return dl_bytes, ul_bytes


def condense_data_estimation(stat_records, minute_gradient):
    """Average consecutive groups of `minute_gradient` records; a shorter final group is averaged as it is."""
    new_stat_records = []
    while stat_records:
        group = stat_records[:minute_gradient]
        temp_record = dict(group[0])
        temp_record['name'] = group[-1]['name']
        temp_record.update(average_group(group))

        stat_records = stat_records[minute_gradient:]
        new_stat_records.append(temp_record)
    return new_stat_records


class ComputeHandler(auth.ApiBase):
    async def post(self):
        action = self.get_argument("action", None)
        machine = self.machine_name("machine", required=False)
        days_duration = self.get_argument("days_duration", None)
        if action is None:
            self.error_respond(400, "No action specified")
            return
        if action == "GET_COST_STATS":
            if machine is not None:
                stat_count, minutes_count = await get_uptime_percentage(machine)
                self.finish({'duration': minutes_count, 'count': stat_count})
            else:
                self.error_respond(400, "No machine name specified.")
        elif action == "FETCH_ESTIMATED_CHARTS":
            if machine is None:
                self.error_respond(400, "No machine name specified.")
            elif days_duration is None:
                self.error_respond(400, "Days duration not specified.")
            else:
                stat_records = await get_db()[stat_name(machine)].find().sort('_id', 1).to_list(None)

                if len(stat_records) < 5:
                    self.finish(dict(status="failure", message="Not enough data"))
                else:
                    i = 0
                    initial_delta_time = dateutil.parser.parse(
                        stat_records[len(stat_records) - 1]['date']) - dateutil.parser.parse(
                        stat_records[0]['date'])

                    if int(get_delta_time_days(stat_records)) < int(days_duration):
                        while int(get_delta_time_days(stat_records)) < int(days_duration):
                            stat_records.append(stat_records[i].copy())
                            cur_date = stat_records[len(stat_records) - 1]['date']
                            cur_date = dateutil.parser.parse(cur_date)
                            cur_date += initial_delta_time + datetime.timedelta(1, 60)
                            stat_records[len(stat_records) - 1]['date'] = cur_date.isoformat()
                            i += 1
                    else:
                        while int(get_delta_time_days(stat_records)) > int(days_duration):
                            stat_records = stat_records[:-1]
                        while int(get_delta_time_seconds(stat_records)) > 60:
                            stat_records = stat_records[:-1]

                    if int(len(stat_records) / 1000) > 0:
                        minute_gradient = int(len(stat_records) / 1000)
                    else:
                        minute_gradient = 1

                    dl_bandwidth, ul_bandwidth = get_bandwidth_usage(stat_records)
                    stat_records = condense_data_estimation(stat_records, minute_gradient)
                    for record in stat_records:
                        record['_id'] = str(record['_id'])
                    self.finish(
                        dict(status="success", stat_data=stat_records,
                             misc_data={'dl_bandwidth': dl_bandwidth, 'ul_bandwidth': ul_bandwidth}))
        else:
            self.error_respond(400, "Not a valid action")


def make_compute_app():
    return Application([(r"/compute", ComputeHandler)], **_app_settings())


def start_compute_server(port):
    asyncio.run(_serve_http(make_compute_app(), port, "Observer Compute"))
