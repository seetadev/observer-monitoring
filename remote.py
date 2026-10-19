import asyncio
import datetime
import json
import platform

import psutil
from tornado.tcpclient import TCPClient

__author__ = 'sdpatro'

prev_recv = -1
prev_sent = -1
prev_read = -1
prev_write = -1


def get_stats(name, nic):
    global prev_recv
    global prev_sent
    global prev_read
    global prev_write

    with open('/proc/uptime', 'r') as f:
        uptime_seconds = float(f.readline().split()[0])

    stats = {'date': datetime.datetime.now().isoformat(), 'name': name,
             'cpu': psutil.cpu_percent(interval=None, percpu=True), 'cpu_count': psutil.cpu_count(),
             'cpu_ctx_switches': psutil.cpu_stats().ctx_switches, 'cpu_interrupts': psutil.cpu_stats().interrupts,
             'ram': psutil.virtual_memory().percent,
             'ram-available': psutil.virtual_memory().available, 'ram-used': psutil.virtual_memory().used,
             'swap': psutil.swap_memory().percent, 'swap-total': psutil.swap_memory().total,
             'swap-used': psutil.swap_memory().used, 'disk_io_read': psutil.disk_io_counters().read_bytes,
             'disk_io_write': psutil.disk_io_counters().write_bytes, 'disk_total': psutil.disk_usage('/').total,
             'disk_used': psutil.disk_usage('/').used,
             'uptime': uptime_seconds}

    nic_list = psutil.net_io_counters(pernic=True)
    nic = nic_list[nic]

    stats['packets_sent'] = [nic.packets_sent, nic.errout]
    stats['packets_recv'] = [nic.packets_recv, nic.errin]
    stats['bytes_recv'] = nic.bytes_recv
    stats['bytes_sent'] = nic.bytes_sent

    if prev_recv != -1:
        stats['dl_rate'] = stats['bytes_recv'] - prev_recv
    else:
        stats['dl_rate'] = 0
    prev_recv = stats['bytes_recv']

    if prev_sent != -1:
        stats['ul_rate'] = stats['bytes_sent'] - prev_sent
    else:
        stats['ul_rate'] = 0
    prev_sent = stats['bytes_sent']

    if prev_read != -1:
        stats['disk_read_rate'] = stats['disk_io_read'] - prev_read
    else:
        stats['disk_read_rate'] = 0
    prev_read = stats['disk_io_read']

    if prev_read != -1:
        stats['disk_write_rate'] = stats['disk_io_write'] - prev_write
    else:
        stats['disk_write_rate'] = 0
    prev_write = stats['disk_io_write']
    return stats


def get_specs(remote_name):
    return {"machine": platform.platform(), "node": platform.node(),
            "architecture": [platform.architecture()[0], platform.architecture()[1]],
            "system": platform.system(), "release": platform.release(), "version": platform.version(),
            "name": remote_name, "memory": psutil.virtual_memory().total}


async def run_remote():
    dest_ip = input("Enter destination IP: ")
    dest_port = int(input("Enter destination port: "))
    remote_name = input("Enter remote name: ")
    nic = input("Enter preferred NIC: ")

    stream = await TCPClient().connect(str(dest_ip), dest_port)
    await stream.write((json.dumps(get_specs(remote_name)) + '\n').encode('utf-8'))
    await asyncio.sleep(1)
    while True:
        text = json.dumps(get_stats(remote_name, nic)) + '\n'
        await stream.write(text.encode('utf-8'))
        print(text, flush=True)
        await asyncio.sleep(1)


def start_remote_server(port):
    print("Observer remote running at port " + port)
    try:
        asyncio.run(run_remote())
    except KeyboardInterrupt:
        print("Keyboard interrupt")
