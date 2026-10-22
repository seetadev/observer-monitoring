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
Starts MongoDB 7 plus the five Observer processes (TCP listener `8889`, dashboard `9000`, live `9002`, compute `9003`, monitor). Ports are published on `127.0.0.1` only (`OBSERVER_PUBLISH_HOST` overrides).

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

## Page-load tests

The **Performance Testing** page runs fixed, parameterised page-load tests: you give a URL, a number of virtual users and a duration, and nothing else. There are no scripts or steps; no user-supplied code runs anywhere. Each virtual user repeatedly loads the page in its own isolated Chromium context (HTTP cache off, so every load is a fresh load) and records the browser's own navigation timings. Results are stored and shown on the page: time to first byte, DOM content loaded, load event, navigation time, document and sub-resource size and count (min / mean / p50 / p95 / max), loads per second, status codes and errors (timeout, network, HTTP). The summary can be downloaded as CSV.

The tests run in the optional `loadtest` worker (it needs Chromium, so it has its own image):

```bash
OBSERVER_LOADTEST_ALLOWED_HOSTS=app.example.org,*.staging.example.org docker compose --profile loadtest up -d --build
```

* **Targets must be listed.** A load test is a request made by the server, so `OBSERVER_LOADTEST_ALLOWED_HOSTS` (host names, `*.example.org`, IP addresses or CIDR ranges) decides what may be tested. With an empty list every test is refused. Every request the browser makes, including each redirect hop and sub-resource, is checked against the same list and blocked otherwise (the result shows how many were blocked and which hosts). Link-local addresses and cloud-metadata names are never allowed, even if listed.
* **Limits**: `OBSERVER_LOADTEST_MAX_CONCURRENCY` (10) and `OBSERVER_LOADTEST_MAX_DURATION` (120 s); one test runs at a time and at most 5 wait in the queue; a page load times out after `OBSERVER_LOADTEST_PAGE_TIMEOUT` (30 s). Results are kept `OBSERVER_LOADTEST_RETENTION_DAYS` (90).
* Starting, cancelling and reading tests needs the admin session (and the XSRF token for changes).
* Without Compose: `pip install -r requirements-loadtest.txt && playwright install chromium && python run.py loadtest`.

## TLS and reverse proxies

Observer itself speaks plain HTTP/TCP; put TLS in front of it.

* **Compose profile `tls`** (nginx): `sh deploy/tls/make-dev-cert.sh` (self-signed, for trying it out) or place your certificate as `deploy/tls/fullchain.pem` and `privkey.pem` (or point `OBSERVER_TLS_CERT_DIR` at a directory holding them), then `docker compose --profile tls up -d --build`.
  * The dashboard is at `https://localhost:8443/` (`OBSERVER_HTTPS_PORT`, bound to `OBSERVER_TLS_BIND`, default `127.0.0.1`). The proxy routes `/_live` and `/_compute` to the live and compute services, so the page is fully same-origin (no mixed content, no cross-origin calls).
  * Agents can use TLS on port `8890`: `OBSERVER_AGENT_PORT=8890 OBSERVER_AGENT_TLS=1` (add `OBSERVER_AGENT_TLS_CA=<your CA file>` for a private or self-signed CA).
  * Without the profile nothing changes: the services stay on `127.0.0.1:9000-9003`.
  * The proxy has the fixed address `172.29.250.2` in the Compose network `172.29.250.0/24` (edit `docker-compose.yml` and `OBSERVER_TRUSTED_PROXIES` together if that range clashes with yours).
* **Your own proxy**: set `OBSERVER_TRUSTED_PROXIES` to its address(es) or network(s). The login limiter then counts failed logins per real client: `X-Forwarded-For` is believed only when the connection comes from a trusted proxy, and is read from the right, so entries a client adds on the left are ignored. Your proxy should overwrite (not append to) the header, as `deploy/nginx/nginx.conf` does. With no trusted proxy configured the header is ignored and the socket address is used. Route `/_live` to the live service (`/live`) and `/_compute` to the compute service (`/compute`), everything else to the dashboard; the page uses the cross-port layout only when it is opened on port 9000.

## Status, alerts and probes

The **monitor** process decides what is up or down on the server (the browser only displays it) and raises alerts. Run exactly one (a database lease keeps an accidental second copy idle). The `Status & Alerts` page shows current states, recent events and warns when no monitor is running.

* **Agents**: down when nothing has arrived for `OBSERVER_DOWN_AFTER_SECONDS` (default 15, and at least three of the agent's own sample intervals).
* **Pull probes** check targets that have no agent: HTTP (`GET`, no redirects, status and optional body text) and TCP (connect). They are listed in a TOML file (`OBSERVER_PROBES_FILE`, Compose mounts `deploy/probes.toml`; see `deploy/probes.example.toml`). Probes cannot be created through the web interface, so a dashboard session can never make the server contact arbitrary addresses. Restart the monitor after editing the file.
* **Alerts**: every transition is logged (`observer.alerts`: warning for down, info for up), stored in the `events` collection (kept `OBSERVER_EVENT_RETENTION_DAYS`, default 30) and, if `OBSERVER_ALERT_WEBHOOK_URL` is set, POSTed as JSON (`event`, `kind`, `name`, `previous`, `since`, `detail`, `time`, and a `text` line that chat tools can show). With `OBSERVER_ALERT_WEBHOOK_SECRET` the request carries `X-Observer-Signature: sha256=<HMAC of the body>`. Delivery is retried three times; only the webhook host is logged, never its path or query. A first observation never alerts, and probes need `fail_after` consecutive failures (default 2) before they are reported down, so a restart or a single blip does not page anyone.

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
