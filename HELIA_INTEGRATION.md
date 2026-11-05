# Helia integration points (plan and scaffold)

Status: **planning + scaffold only.** `integrations/helia.py` is a tested client; nothing in Observer imports it, no route calls it and the Dockerfile does not copy it. This document says where Helia would plug in, where it would **not**, and what is still undecided.

Observer never embeds Helia. It would call the **Helia sidecar** (`meshkit-sidecar-helia`, in `seetadev/Tornado-Backend-Stack`) over HTTP from a server-side process. The agents on monitored machines never talk to it.

## The sidecar contract (read from the source at `8afe218`, confirmed by running it, re-verified at `a114973`)

| Call | Behaviour |
|---|---|
| `POST /upload` (raw bytes) | `200 {"cid": "..."}`. Over `MESHKIT_HELIA_MAX_UPLOAD_MB` (default **20**) -> `413`. |
| `GET /retrieve/<cid>` | `200` raw bytes. Malformed CID -> `400`. A CID it does not hold -> **`504` after 5 s** (not 404) in the default offline mode. |
| `GET /list` | every CID uploaded since the sidecar started (global, not per user). |
| `GET /health` | `200 {"status":"ok","peerId":...}`, unauthenticated. |
| auth | every route except `/health` needs `X-Meshkit-Helia-Token: <MESHKIT_HELIA_SHARED_SECRET>` -> otherwise `401`. The sidecar publishes no host port in its own compose file: it is meant to be reached over a docker network. |

Not in the sidecar: **delete**, per-user ownership, and (offline mode) any replication. Payloads over one chunk (1 MiB) get a DAG root CID that cannot be recomputed locally, so keep a separate `sha256` for anything you must verify.

The sidecar exists only in the open PR [seetadev/Tornado-Backend-Stack#15](https://github.com/seetadev/Tornado-Backend-Stack/pull/15) (a bug where an upload without `Content-Type` was answered with the CID of an empty file is fixed there; the client always sends `application/octet-stream`, so it never depended on the fix). The repository is private.

## What the scaffold provides

`integrations/helia.py`: `HeliaClient(base_url, secret, ...)` with `await put(bytes) -> cid`, `await get(cid) -> bytes`, `await health() -> bool`, on `tornado.httpclient.AsyncHTTPClient` (Observer is already a Tornado app).

- Checked locally before any request: CID shape, payload size (default 20 MiB), non-empty bytes.
- The secret goes only in `X-Meshkit-Helia-Token`; never in `repr()`, a log line or an error. Redirects are never followed, so the token cannot be forwarded elsewhere.
- Errors map as the Tornado proxy maps them: unreachable / 502 / 503 / 401 -> `HeliaUnavailable` (502; a rejected secret is the subclass `HeliaMisconfigured`), slow or 504 -> `HeliaTimeout` (504), bad CID -> `HeliaInvalidCid` (400), too large -> `HeliaPayloadTooLarge` (413), 404 -> `HeliaNotFound`, anything else -> `HeliaError`.
- Upload replies are validated: a reply without a valid CID is an error, not trusted.

Configuration (`HeliaClient.from_env()` returns `None` when `HELIA_SIDECAR_URL` is unset = Helia off):

| Variable | Meaning |
|---|---|
| `HELIA_SIDECAR_URL` | e.g. `http://meshkit-sidecar-helia:5052`. Unset = disabled. |
| `HELIA_SIDECAR_SECRET` | must equal the sidecar's `MESHKIT_HELIA_SHARED_SECRET`. Server-side only; plain `http://` only on a private network. |
| `HELIA_API_PREFIX`, `HELIA_HEALTH_PATH` | `""` and `/health` for the sidecar itself; `/meshkit-helia` through the Tornado proxy (which has no caller authentication yet). |
| `HELIA_MAX_UPLOAD_MB`, `HELIA_CONNECT_TIMEOUT`, `HELIA_REQUEST_TIMEOUT` | 20, 3 s, 8 s. |
| `HELIA_ARCHIVE_ENABLED` | **`false` by default.** Archiving is opt-in; with it off the retention TTLs behave exactly as they do today. Only `1`/`true`/`yes`/`on` enable it (a typo does not). Read by `config.py`; there is no archive job yet, so today it gates nothing. |

## Where Helia fits in Observer

What Observer stores (from `db.py` / `config.py`): raw 1-second samples (`live_*`, condensed away and TTL-cleaned), **1-minute statistics** per machine (`stat_*`, kept forever unless `OBSERVER_STAT_RETENTION_DAYS` is set), **alert events** (`events`, TTL `OBSERVER_EVENT_RETENTION_DAYS`, default 30 days), **page-load test results** (`loadtests`, TTL `OBSERVER_LOADTEST_RETENTION_DAYS`, default 90 days), and CSV/XLSX **exports** written to `OBSERVER_FILES_DIR` and served by the authenticated `/files/<name>`.

### 1. Archive history before the retention TTL removes it (the real fit)
MongoDB's TTL indexes delete expired documents silently; there is no hook. Today the 30-day alert history and the 90-day load-test history are simply gone afterwards, and a long retention for statistics means a growing database. An **archive job** would write closed time windows (say one UTC day per machine for statistics, one day of events, each finished load test) as a file, `put()` it, and record `{kind, machine, from, to, rows, bytes, sha256, cid, createdAt}` in a small `archives` collection. Restoring or auditing is `get(cid)` plus a `sha256` check.

- Why content addressing helps: an archived window cannot be altered without changing its CID, so anyone who recorded a CID somewhere else (a ticket, a report, a log line) can later prove what the monitor saw (for example, "was the server really down at 03:10?"). The `archives` rows in MongoDB are not tamper-evident by themselves; the CID only proves something against a copy kept elsewhere.
- Windows must be small enough for the 20 MiB cap: a day of 1-minute statistics for one machine is 1,440 rows per collection; `MAX_EXPORT_ROWS` (100,000) already bounds exports. Split a window that would not fit.
- The job is **opt-in (`HELIA_ARCHIVE_ENABLED`, off by default)**: with the flag off nothing is archived and the TTLs delete exactly as they do today (a test pins that the TTL indexes are identical with the flag on or off). It must **run before the TTL fires** (archive a window once it is closed, not at the end of retention) and must never make deletion depend on Helia: Mongo keeps its TTL; the archive is an additional copy. If Helia is down the job logs one warning and retries later; an `archives` row exists only after a successful `put()`.
- Runs best as its own small process next to `monitor.py` (it must not block the dashboard or the TCP listener); the admin CLI (`python run.py ...`) would get `archive` and `restore` commands. Neither exists.

### 2. Keep an export or a load-test report (weak unless the sidecar is online)
`export_results` and the load-test CSV are one-off files. After writing one, Observer could `put()` it and show the CID next to the download. That is only more useful than the download itself if somebody else can fetch the CID, which needs the sidecar in `online` mode or a gateway (see the questions). In the default offline mode it is a second copy on the same host.

### Where it does **not** fit
- **The agent sample stream.** One sample per second per machine over a persistent TCP connection is a time series; an immutable blob store with a 5 s miss penalty is the wrong tool and would add a failure mode to the hot path.
- **Live dashboards, alerts, probes.** They need low latency and a mutable current state, which is what MongoDB is for.
- **Authentication state** (admin hash, agent token digests): secrets must not be put in a store with no delete.

## Fallback on 502 / 504
Helia is an add-on, never the only copy: Mongo (and the local export file) stays the source of truth.

- `HeliaUnavailable` (502) or `HeliaTimeout` (504) while archiving -> log one warning (no secret, no payload), leave the window unarchived, retry on the next run, and **raise an alert event if a window is about to pass its TTL unarchived**.
- A `504` on `get()` can mean "slow" or "this sidecar does not hold that CID" (offline mode), so a restore treats both as "not available here".
- `HeliaMisconfigured` (secret rejected) is a deployment bug: surface it on `/status`, not just in a log.

## Is encryption needed?
Observer's data is not personal data, but it is **sensitive infrastructure data**: host names and IP addresses, load and process patterns, outage times, and the URLs people load-test (often internal). Reasons to encrypt before `put()`:
- The sidecar `/list` is global and the secret is shared: **any other app that holds the secret** (the Tornado backend and its clients) can list and read every CID, and the sidecar has no per-owner access control.
- In `online` mode a stored CID is announced to peers.
- There is no delete, so a retention promise ("30 days") can only be kept by throwing the key away (crypto-shredding).

Recommendation: encrypt (AES-256-GCM, a fresh nonce per object, the window's identity as authenticated data) unless the sidecar is dedicated to Observer **and** offline. The Rhinestone scaffold (`AspiringDevelopers/Rhinestone`, branch `feature/helia-integration-points`) has a tested envelope module (`integrations/helia_crypto.py`) that could be copied; it is not duplicated here until the decision is made.

The retention conflict is the first open question below; archiving stays off until it is answered.

## Open questions

**1. Retention conflict (decide before anyone enables archiving).** The TTLs exist to *remove* data: events after 30 days, load-test results after 90, statistics if `OBSERVER_STAT_RETENTION_DAYS` is set. The sidecar has **no delete**, so an archive outlives every one of those settings. Two coherent answers:

| | Plaintext archive | Encrypted archive + scheduled key destruction |
|---|---|---|
| What is stored | the CSV/JSON window as is | AES-256-GCM ciphertext; one key per retention bucket (for example per month), kept by Observer, not the sidecar |
| Retention promise | **broken by design**: the data is retained for as long as the sidecar's volume exists, whatever the TTL says; this must be stated plainly to anyone who relies on the 30/90-day figures | **kept**: when a bucket's retention ends the key is destroyed, and the ciphertext left in the sidecar is unreadable (crypto-shredding) |
| Who can read it | anyone holding the sidecar secret (global `/list`, shared secret), and peers in `online` mode | only Observer, with the key; losing the key loses the archive |
| Cost | none | key storage and rotation, a destruction job that must itself be reliable and audited, no de-duplication |

Plaintext is only defensible if the sidecar is dedicated to Observer, offline, and the data is accepted as permanently retained. Anything else points at the encrypted option. Either way the archive's own retention is a new setting someone has to choose.

**2.** Is there a Helia sidecar for Observer to use, and where does it run (this repo's Compose has no place for a private-repo image; the Tornado-Backend-Stack network is shared with other apps)?
**3.** Offline (one node, one volume: nothing replicates, a lost volume loses the archive) or online (replication, but CIDs become visible)? Without online, is an archive better than a plain file on a backup volume? Honest answer: only the content addressing (a verifiable CID) and a shareable index are new.
**4.** Who may restore or read an archive: the single admin only (as today)?
**5.** Should an `archives` row be shown on the dashboard (a "history" page) or stay an admin-CLI tool?

## Running the opt-in integration test (real sidecar)

```bash
git clone git@github.com:seetadev/Tornado-Backend-Stack.git ../Tornado-Backend-Stack
git -C ../Tornado-Backend-Stack checkout a114973
export HELIA_SIDECAR_BUILD_CONTEXT=$PWD/../Tornado-Backend-Stack/meshkit-sidecar-helia
export HELIA_SIDECAR_SECRET=$(openssl rand -hex 32)
docker compose -f tests/integration/docker-compose.helia.yml up -d --build --wait
HELIA_SIDECAR_URL=http://127.0.0.1:5052 pytest tests/integration -v -s
docker compose -f tests/integration/docker-compose.helia.yml down -v
```

Without `HELIA_SIDECAR_URL` those tests are skipped (CI never needs the sidecar). The regular unit tests (`tests/test_helia_client.py`) use `tests/helia_mock.py`, a **mock** of the contract above, not the real sidecar. Against the real sidecar at `a114973`: 9 passed, 1 xpassed.

When this is wired in, the `Dockerfile` needs `COPY integrations ./integrations` (it copies a fixed list of modules today). Note `tests/test_packaging.py` only checks top-level `*.py` modules, so it will **not** notice a missing `integrations/` directory: extend it when wiring.
