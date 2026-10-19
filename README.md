# observer-monitoring
A monitoring tool for checking the status of local/remote servers, based on <a href="https://github.com/giampaolo/psutil">psutil</a>, <a href="https://github.com/tornadoweb/tornado">Tornado</a> and <a href="https://github.com/mongodb/mongo">MongoDB</a>.

> **Status:** Python 3.12, Tornado 6.5, motor (async MongoDB driver) and MongoDB 7. The dashboard still has **no authentication yet**: keep the ports on loopback or a private network until the access-control changes land.

## How-to-run

### Root server (Docker Compose)
```bash
docker compose up -d --build
```
Starts MongoDB 7 plus the five Observer processes (TCP listener `8889`, dashboard `9000`, simulator `9001`, live `9002`, compute `9003`). Ports are published on `127.0.0.1` only (`OBSERVER_PUBLISH_HOST` overrides).

### Root server (without Docker)
```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
# a MongoDB 7 reachable at OBSERVER_MONGO_URL (default mongodb://127.0.0.1:27017)
bash run-root.sh
```

Configuration (environment variables): `OBSERVER_MONGO_URL`, `OBSERVER_DB_NAME`, `OBSERVER_BIND` (bind address, default `127.0.0.1`), `OBSERVER_DEBUG`, `OBSERVER_FILES_DIR`.

### Remote senders
Run on each machine you want to monitor (Linux; Python 3.12 with `psutil` and `tornado`):
```bash
$ bash run-remote.sh
Observer remote running at port 8888
Enter destination IP: 
203.0.113.10
Enter destination port: 
8889
Enter remote name: 
foobar-production
Enter preferred NIC: 
eth0
```
### Dashboard navigation
+ **Live monitoring** : [ROOT_IP_ADDRESS]:9000/live
+ **Statistical data** : [ROOT_IP_ADDRESS]:9000/stats
+ **Performance testing** : [ROOT_IP_ADDRESS]:9000/perf

<a href="http://imgur.com/KALI2zL"><img src="http://i.imgur.com/KALI2zL.png" title="source: imgur.com" /></a>
<br/>
<a href="http://imgur.com/zRYCVqf"><img src="http://i.imgur.com/zRYCVqf.png" title="source: imgur.com" /></a>
<br/>
<a href="http://imgur.com/oTAL194"><img src="http://i.imgur.com/oTAL194.png" title="source: imgur.com" /></a>
<br/>
<a href="http://imgur.com/sSYG69F"><img src="http://i.imgur.com/sSYG69F.png" title="source: imgur.com" /></a>
<br/>
<a href="http://imgur.com/NBlxMlm"><img src="http://i.imgur.com/NBlxMlm.png" title="source: imgur.com" /></a>
<br/>
<a href="http://imgur.com/SIfXj2z"><img src="http://i.imgur.com/SIfXj2z.png" title="source: imgur.com" /></a>
<br/>
<a href="http://imgur.com/WxKtrsf"><img src="http://i.imgur.com/WxKtrsf.png" title="source: imgur.com" /></a>
