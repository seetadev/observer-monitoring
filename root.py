import asyncio
import base64
import calendar
import datetime
import json
import logging
import os
import queue
import threading
import time

import dateutil.parser
import tornado.iostream
import tornado.web
import xlsxwriter
from PIL import Image
from tornado.httpserver import HTTPServer
from tornado.iostream import StreamClosedError
from tornado.tcpserver import TCPServer
from tornado.web import Application

import auth
import config
import uimodules
from db import get_db, live_name, stat_name, tests_name

__author__ = 'sdpatro'

log = logging.getLogger('observer')

MAX_LINE_BYTES = 64 * 1024
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
        data_json = json.loads(data)
        if not isinstance(data_json, dict) or data_json.get('name') != agent:
            raise ValueError("record does not belong to the authenticated agent")

        if 'node' in data_json:
            await update_machine(data_json, ip)

        else:
            live_coll = db[live_name(data_json['name'])]
            stat_coll = db[stat_name(data_json['name'])]

            await update_machine_time(data_json["name"])

            await live_coll.insert_one(data_json)
            await condense(stat_coll, live_coll)


async def update_machine(json_data, machine_ip):
    machines = get_db()["machines"]
    machine_name = json_data["name"]
    machine = await machines.find_one({'name': machine_name})
    if machine is None:
        await machines.insert_one(
            {'name': machine_name, 'ip': machine_ip, 'last_online': str(datetime.datetime.now().isoformat()),
             'machine': json_data['machine'], 'node': json_data['node'], 'architecture': json_data['architecture'],
             'system': json_data['system'], 'release': json_data['release'], 'version': json_data['release'],
             'memory': json_data['memory']})
    else:
        await machines.update_one({'name': machine_name}, {'$set': {'ip': machine_ip}})


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

        await live_coll.delete_many({'_id': {'$in': [record['_id'] for record in live_data]}})
        print("Condensing...", flush=True)
        await stat_coll.insert_one(stat)


async def _serve_tcp(server, port, label):
    auth.validate_config()
    await init_instance_pricing()
    await auth.ensure_indexes()
    server.listen(int(port), address=config.BIND_ADDRESS)
    print(label + " running at port " + str(port), flush=True)
    await asyncio.Event().wait()


# Fire up the TCP server.
def start_listener(port):
    asyncio.run(_serve_tcp(TCPListener(), port, "Observer TCP"))


########################################################################


class ObserverDriver:
    def __init__(self, driver, test_name, machine_name):
        self.web_driver = driver
        self.test_name = test_name
        self.machine_name = machine_name
        self.steps = []
        self.snaps = []

    def _wait_for_load(self, wait_till_loaded=None):
        while self.web_driver.execute_script('return document.readyState;') != 'complete':
            time.sleep(0.05)
        if wait_till_loaded is not None:
            while not self.web_driver.execute_script('return ' + wait_till_loaded + ';'):
                time.sleep(0.05)

    def set_web_driver(self, driver):
        self.web_driver = driver

    def set_dimensions(self, height, width):
        self.web_driver.set_window_size(height, width)

    def snap(self, height=None, width=None, ):
        snap_date = str(datetime.datetime.now().isoformat())
        self.web_driver.set_window_position(0, 0)
        if height is not None and width is not None:
            self.web_driver.set_window_size(height, width)
        snap_name = "files_buffer/" + self.machine_name + "_" + self.test_name + "_" + snap_date + ".jpg"
        self.web_driver.save_screenshot(snap_name)
        with open(snap_name, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('ascii')
            self.snaps.append({'snap_name': snap_name, 'snap_content': encoded_string})

    def go_to(self, url, record=False, wait_till_loaded=None):
        start_time = datetime.datetime.now()
        self.web_driver.get(url)
        self._wait_for_load(wait_till_loaded)
        end_time = datetime.datetime.now()
        self.steps.append(
            dict(action="go_to", target=url, startTime=start_time.isoformat(), endTime=end_time.isoformat(),
                 record=record))

    def button_click_id(self, button_id, record=False, wait_till_loaded=None):
        from selenium.webdriver.common.by import By
        start_time = datetime.datetime.now()
        self.web_driver.find_element(By.ID, str(button_id)).click()
        self._wait_for_load(wait_till_loaded)
        end_time = datetime.datetime.now()
        self.steps.append(
            dict(action="button_click", target=button_id, startTime=start_time.isoformat(),
                 endTime=end_time.isoformat(), record=record))

    def button_click_class(self, class_name, btn_index, record=False, wait_till_loaded=None):
        from selenium.webdriver.common.by import By
        start_time = datetime.datetime.now()
        btn_list = self.web_driver.find_elements(By.CLASS_NAME, class_name)
        btn_list[btn_index].click()
        self._wait_for_load(wait_till_loaded)
        end_time = datetime.datetime.now()
        self.steps.append(
            dict(action="button_click", target=class_name + " " + str(btn_index), startTime=start_time.isoformat(),
                 endTime=end_time.isoformat(), record=record))

    def submit_form(self, form_element_id, record=False, wait_till_loaded=None):
        from selenium.webdriver.common.by import By
        start_time = datetime.datetime.now()
        self.web_driver.find_element(By.ID, form_element_id).submit()
        self._wait_for_load(wait_till_loaded)
        end_time = datetime.datetime.now()
        self.steps.append(
            dict(action="form_submit", target=form_element_id, startTime=start_time.isoformat(),
                 endTime=end_time.isoformat(), record=record))

    def fill_form_element(self, form_element_id, input_text, record=False):
        from selenium.webdriver.common.by import By
        start_time = datetime.datetime.now()
        self.web_driver.find_element(By.ID, form_element_id).send_keys(input_text)
        self._wait_for_load()
        end_time = datetime.datetime.now()
        self.steps.append(
            dict(action="fill_form_element", target=form_element_id, startTime=start_time.isoformat(),
                 endTime=end_time.isoformat(), record=record))

    def close_driver(self):
        self._wait_for_load()
        self.web_driver.close()
        self.web_driver.quit()

    def check_element(self, element_id):
        from selenium.common.exceptions import NoSuchElementException
        from selenium.webdriver.common.by import By
        self._wait_for_load()
        try:
            self.web_driver.find_element(By.ID, element_id)
        except NoSuchElementException:
            return False
        return True

    def get_steps(self):
        return self.steps

    def get_snaps(self):
        return self.snaps


def get_excel_column(number):
    char_list = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T',
                 'U', 'V', 'W', 'X', 'Y', 'Z']
    rem = number
    result = ""
    while True:
        if rem > 26:
            result = result + char_list[(rem - (26 * int(rem / 26))) - 1]
            rem = int(rem / 26)
        else:
            result = result + char_list[rem - 1]
            break
    return result


class ApiHandler(auth.ApiBase):
    def requires_auth(self):
        return self.get_argument("action", None) not in ("LOGIN_ADMIN", "LOGOUT_ADMIN")

    async def login(self):
        password = self.get_argument("password", "")
        key = self.request.remote_ip
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
                instance_pricing['_id'] = str(instance_pricing['_id'])
                self.finish(dict(response_data=json.dumps(instance_pricing)))

        elif action == "GET_SPECS":
            machine_name = self.get_argument("machine_name", None)
            if machine_name is None:
                self.error_respond(400, "Machine name not provided.")
            else:
                machine = await db["machines"].find_one({'name': machine_name})
                self.finish({'machine': machine['machine'], 'node': machine['node'], 'version': machine['version'],
                             'release': machine['release'], 'system': machine['system'],
                             'architecture': machine['architecture'], 'memory': machine['memory']})

        elif action == "GET_STAT_DATA":
            client_name = self.get_argument("client-name")
            stat_data = []
            async for record in db[stat_name(client_name)].find():
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
            async for remote_machine in db["machines"].find():
                remote_machines_list.append({'name': remote_machine['name'], 'ip': remote_machine['ip'],
                                             'last_online': remote_machine['last_online']})
            self.finish((dict(remoteMachines=json.dumps({'machines': remote_machines_list}))))

        elif action == "SAVE_TEST":
            test_code = self.get_argument("testCode", None)
            test_name = self.get_argument("testName", None)
            machine_name = self.get_argument("machine", None)

            if test_code is None or test_name is None or machine_name is None:
                self.finish((dict(status="failure", output="Missing argument in request")))
                return

            try:
                test_object = {'machine': machine_name, 'script': test_code, 'name': test_name}
                await db[tests_name(machine_name)].insert_one(test_object)
                status = "success"
                output = test_name + " for " + machine_name + " successfully saved."
            except Exception as e:
                output = str(e)
                status = "failure"
            self.finish((dict(status=status, output=output)))

        elif action == "FETCH_TESTS_LIST":
            machine_name = self.get_argument("machine", None)
            if machine_name is None:
                self.error_respond(400, "Arguments missing")
                return
            try:
                response_object = []
                async for test in db[tests_name(machine_name)].find():
                    response_object.append({'name': test['name'], 'machine': test['machine']})
                self.finish(dict(response_data=json.dumps({'tests': response_object})))
            except Exception as e:
                self.error_respond(500, "Something went wrong: " + str(e))

        elif action == "FETCH_TEST":
            machine_name = self.get_argument("machine", None)
            test_name = self.get_argument("test_name", None)
            if machine_name is None or test_name is None:
                self.error_respond(400, "Arguments missing")
                return
            test = await db[tests_name(machine_name)].find_one({"name": test_name})
            try:
                self.finish(dict(response_data=json.dumps({'script': test['script']})))
            except Exception as e:
                self.error_respond(500, "Something went wrong: " + str(e))

        elif action == "LOGIN_ADMIN":
            await self.login()

        elif action == "LOGOUT_ADMIN":
            self.end_session()
            self.finish(dict(auth="False"))

        elif action == "DELETE_REMOTE_MACHINE":
            machine_name = self.get_argument("machine_id")
            try:
                await db[live_name(machine_name)].drop()
                await db[stat_name(machine_name)].drop()
                await db["machines"].delete_many({"name": machine_name})
                self.finish(dict(status="True"))
            except Exception as e:
                self.error_respond(500, "Something went wrong: " + str(e))

        elif action == "SAVE_TEST_RESULT_AS":
            json_data = json.loads(self.get_argument("jsonData"))
            test_name = self.get_argument("test_name")
            file_type = self.get_argument("file_type")

            if file_type == "XLSX":

                live_data = json_data["live_data"]
                snaps = json_data["snaps_id"]

                workbook_name = test_name + "_" + datetime.datetime.now().isoformat()

                workbook = xlsxwriter.Workbook(os.path.join(config.FILES_DIR, workbook_name + ".xlsx"))
                worksheet = workbook.add_worksheet()
                bold_header = workbook.add_format({'bold': True, 'font_color': '#0b9f51'})
                large_bold_green = workbook.add_format({'bold': True, 'font_size': 18, 'font_color': '#009245'})
                large_bold_blue = workbook.add_format({'bold': True, 'font_size': 18, 'font_color': '#005392'})

                worksheet.set_row(0, 30)
                worksheet.write("A1", "Stats", large_bold_green)

                record_offset = 3
                property_header_offset = 2
                col = 0

                for prop in live_data[0]:
                    worksheet.write(get_excel_column(col + 1) + str(property_header_offset + 1), str(prop),
                                    bold_header)
                    worksheet.set_column(get_excel_column(col + 1) + ":" + get_excel_column(col + 1), 20)
                    col += 1
                for j, record in enumerate(live_data):
                    for k, value in enumerate(record.values()):
                        worksheet.write(get_excel_column(k + 1) + str(j + 1 + record_offset), str(value))

                worksheet.set_row(len(live_data) + 4, 30)
                worksheet.write("A" + str(len(live_data) + 5), "Snaps", large_bold_blue)

                images_offset = len(live_data) + 5
                last_cell_height = 0
                for snap in snaps:
                    worksheet.insert_image("A" + str(images_offset + last_cell_height + 2), snap,
                                           {'x_scale': 0.5, 'y_scale': 0.5})
                    im = Image.open(snap)
                    width, height = im.size
                    last_cell_height += int(height / 30)

                workbook.close()
                self.finish(dict(file_name=workbook_name))

            elif file_type == "CSV":
                live_data = json_data["live_data"]
                csv_file_name = test_name + "_" + datetime.datetime.now().isoformat() + ".csv"
                with open(os.path.join(config.FILES_DIR, csv_file_name), 'w') as csv_fp:
                    csv_fp.write(",".join(live_data[0]) + "\n")
                    for record in live_data:
                        csv_fp.write(",".join(str(value) for value in record.values()) + "\n")
                self.finish(dict(file_name=csv_file_name))
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
                        (r"/admin", AdminHandler),
                        (r"/files/(.*)", auth.AuthenticatedStaticFileHandler, {"path": config.FILES_DIR}),
                        (r"/fonts/(.*)", tornado.web.StaticFileHandler, {"path": config.FONTS_DIR}),
                        (r"/(.*)", tornado.web.StaticFileHandler, {"path": config.STATIC_DIR})],
                       **_app_settings(ui_modules=uimodules))


async def _serve_http(app, port, label):
    auth.validate_config()
    await init_instance_pricing()
    server = HTTPServer(app)
    server.listen(int(port), address=config.BIND_ADDRESS)
    print(label + " running at port " + str(port), flush=True)
    await asyncio.Event().wait()


def start_dash_server(port):
    asyncio.run(_serve_http(make_dash_app(), port, "Observer Dashboard"))


##############################################################


def runTests(test_code, test_name, machine_name, output_queue, test_error_queue):
    from selenium import webdriver
    driver = ObserverDriver(webdriver.Chrome(), test_name, machine_name)
    try:
        scope = dict(globals())
        scope.update(driver=driver, test_name=test_name, machine_name=machine_name)
        exec(test_code, scope)
    except Exception as e:
        test_error_queue.put(str(e))

    driver.close_driver()
    test_output = {'steps': driver.get_steps(), 'snaps': driver.get_snaps()}
    output_queue.put(test_output)


def _run_test_instances(test_code, test_name, machine_name, instances_count):
    status = "success"
    queue_list = []
    test_error_queue = queue.Queue()
    try:
        thread_instances = []
        for i in range(0, instances_count):
            queue_list.append(queue.Queue())
            thread_instances.append(
                threading.Thread(target=runTests,
                                 args=(test_code, test_name, machine_name, queue_list[i], test_error_queue)))
            thread_instances[i].start()
        for thread in thread_instances:
            thread.join()
    except Exception:
        status = "failure"

    test_error_msg = None
    while not test_error_queue.empty():
        test_error_msg = test_error_queue.get()

    if test_error_msg:
        status = "failure"

    test_output = []
    if status == "success":
        for q in queue_list:
            test_output_unit_list = []
            while not q.empty():
                test_output_unit_list.append(q.get())
            test_output.append(test_output_unit_list)
    return status, test_error_msg, test_output


class SimHandler(auth.ApiBase):
    async def post(self):
        action = self.get_argument("action", None)
        if action is None:
            self.error_respond(400, "No action specified")
            return
        if action == "RUN_TEST":
            test_name = self.get_argument("testName", None)
            machine_name = self.get_argument("machineName", None)
            test_code = self.get_argument("testCode", None)
            instances_count = int(self.get_argument("instancesCount", None))
            status, test_error_msg, test_output = await asyncio.to_thread(
                _run_test_instances, test_code, test_name, machine_name, instances_count)
            if status == "success":
                self.finish((dict(status=status, output=json.dumps(test_output))))
            else:
                self.finish((dict(status=status, msg=test_error_msg)))


def make_sim_app():
    return Application([(r"/sim", SimHandler)], **_app_settings())


def start_sim_server(port):
    asyncio.run(_serve_http(make_sim_app(), port, "Observer Simulator"))


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
                client_name = self.get_argument("client-name")
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
    new_stat_records = []
    while len(stat_records) > minute_gradient:
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
        machine = self.get_argument("machine", None)
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
                stat_records = await get_db()[stat_name(machine)].find().to_list(None)

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
