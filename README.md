# observer-monitoring
A monitoring tool for checking the status of local/remote servers, based on <a href="https://github.com/giampaolo/psutil">psutil</a>, <a href="https://github.com/tornadoweb/tornado">Tornado</a> and <a href="https://github.com/mongodb/mongo">MongoDB</a>.

> **Status:** Python 3.12, Tornado 6.5, PyMongo's native async client and MongoDB 7. The dashboard and its APIs require an admin login; agents must present a token.

## Access control

* **Admin**: one account. Set `OBSERVER_ADMIN_PASSWORD_HASH` (create it with `python run.py hash-password`; minimum 12 characters) and `OBSERVER_COOKIE_SECRET` (`openssl rand -hex 32`, same value for every process). Production (`OBSERVER_ENV=production`, the Compose default) refuses to start without them. Sessions are signed, expiring, HttpOnly cookies and every state-changing request needs the XSRF token.
* **Agents**: create one token per machine: `python run.py agent-add NAME` (Compose: `docker compose run --rm listener agent-add NAME`). The token is shown once and stored only as a digest. `agent-list` and `agent-revoke NAME` manage them. Unknown or wrong tokens are disconnected.

### Root server (Docker Compose)
```bash
cp .env.example .env   # fill in OBSERVER_COOKIE_SECRET and OBSERVER_ADMIN_PASSWORD_HASH
docker compose up -d --build
```
Starts MongoDB 7 plus the four Observer processes (TCP listener `8889`, dashboard `9000`, live `9002`, compute `9003`). Ports are published on `127.0.0.1` only (`OBSERVER_PUBLISH_HOST` overrides).

### Root server (without Docker)
```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
# a MongoDB 7 reachable at OBSERVER_MONGO_URL (default mongodb://127.0.0.1:27017)
bash run-root.sh
```

Configuration (environment variables): `OBSERVER_MONGO_URL`, `OBSERVER_DB_NAME`, `OBSERVER_BIND` (bind address, default `127.0.0.1`), `OBSERVER_DEBUG`, `OBSERVER_FILES_DIR`, `OBSERVER_LIVE_TTL_SECONDS` (default 3600: removes a raw-sample buffer left behind by a stopped agent), `OBSERVER_STAT_RETENTION_DAYS` (default 0 = keep 1-minute statistics forever), and the access-control variables listed in `.env.example`.

### Agent (on each machine you monitor)

The agent is one file, `agent.py`, and needs only Python 3.11+ and `psutil`. It runs without prompts, takes its settings from environment variables (or a TOML file), reconnects with exponential back-off when the server is unreachable, and works on Linux, macOS and Windows.

1. On the Observer server, create the agent's token (shown once): `python run.py agent-add web-01`.
2. On the monitored machine:
   ```bash
   sudo install -d /opt/observer-agent /etc/observer-agent
   sudo python3 -m venv /opt/observer-agent/venv && sudo /opt/observer-agent/venv/bin/pip install -r requirements-agent.txt
   sudo install -m 644 agent.py /opt/observer-agent/
   sudo install -m 644 deploy/agent.env.example /etc/observer-agent/agent.env   # edit server / name
   sudo sh -c 'umask 077; cat > /etc/observer-agent/token'                      # paste the token, Ctrl-D
   sudo install -m 644 deploy/observer-agent.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now observer-agent
   journalctl -u observer-agent -f
   ```
3. Or run it directly: `OBSERVER_AGENT_SERVER=host OBSERVER_AGENT_NAME=web-01 OBSERVER_AGENT_TOKEN=... python agent.py` (`--check` validates the configuration and exits).

| Variable | Default | Meaning |
|---|---|---|
| `OBSERVER_AGENT_SERVER` / `_PORT` | `127.0.0.1` / `8889` | Observer TCP listener |
| `OBSERVER_AGENT_NAME` | host name | must equal the name the token was created for |
| `OBSERVER_AGENT_TOKEN` or `_TOKEN_FILE` | (required) | the token; a file is preferred (mode 600). It is never accepted inside the TOML file |
| `OBSERVER_AGENT_NIC` | `auto` | `auto` = all non-loopback interfaces, or an interface name |
| `OBSERVER_AGENT_INTERVAL` | `1` | seconds between samples |
| `OBSERVER_AGENT_BACKOFF_MAX` | `60` | longest wait between reconnect attempts |
| `OBSERVER_AGENT_TLS`, `_TLS_CA` | off | TLS to a TLS-terminating proxy; optional CA file |
| `OBSERVER_AGENT_CONFIG` | (none) | TOML file with the same keys in lower case (`deploy/agent.toml.example`) |

The previous interactive `run-remote.sh` is gone; `python run.py agent` runs the same agent.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
docker run -d --name observer-test-mongo -p 127.0.0.1:27018:27017 mongo:7
TEST_MONGO_URL=mongodb://127.0.0.1:27018 .venv/bin/pytest -q -rx
.venv/bin/ruff check .
```
Each test uses its own throw-away database (`observer_test_*`), dropped afterwards. Tests marked `xfail(strict=True)` document known defects; the pull request that fixes a defect removes its marker.

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
