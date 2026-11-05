"""Opt-in: the client against the REAL Helia sidecar. Skipped unless HELIA_SIDECAR_URL is set.

    export HELIA_SIDECAR_SECRET=$(openssl rand -hex 32)
    docker compose -f tests/integration/docker-compose.helia.yml up -d --build --wait
    HELIA_SIDECAR_URL=http://127.0.0.1:5052 pytest tests/integration -v -s
    docker compose -f tests/integration/docker-compose.helia.yml down -v

HELIA_MAX_UPLOAD_MB must match the sidecar's (the compose file uses 1). Takes ~10 s: one test waits for the sidecar's
own 5 s retrieve timeout.
"""
import base64
import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.request

import pytest
import tornado.testing

from integrations.helia import (
    HeliaClient, HeliaInvalidCid, HeliaMisconfigured, HeliaPayloadTooLarge, HeliaTimeout, HeliaUnavailable, is_valid_cid,
)

URL = os.environ.get("HELIA_SIDECAR_URL", "").strip()
SECRET = os.environ.get("HELIA_SIDECAR_SECRET", "")
LIMIT = int(float(os.environ.get("HELIA_MAX_UPLOAD_MB", "1")) * 1024 * 1024)

pytestmark = pytest.mark.skipif(not URL, reason="set HELIA_SIDECAR_URL (and HELIA_SIDECAR_SECRET) to run against the real sidecar")


def raw_cid(data):
    """CIDv1 / raw / sha2-256, computed locally."""
    return "b" + base64.b32encode(bytes([0x01, 0x55, 0x12, 0x20]) + hashlib.sha256(data).digest()).decode().lower().rstrip("=")


def raw_http(method, path, body=None, token=SECRET, content_type="application/octet-stream"):
    """A request that bypasses the client, to see what the sidecar itself answers."""
    req = urllib.request.Request(URL.rstrip("/") + path, data=body, method=method)
    if token:
        req.add_header("X-Meshkit-Helia-Token", token)
    if body is not None and content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TestRealSidecar(tornado.testing.AsyncTestCase):
    def setUp(self):
        super().setUp()
        self.client = HeliaClient.from_env()

    def tearDown(self):
        self.client.close()
        super().tearDown()

    @tornado.testing.gen_test(timeout=30)
    async def test_health(self):
        assert await self.client.health() is True

    @tornado.testing.gen_test(timeout=30)
    async def test_put_get_round_trip_and_deterministic_cid(self):
        data = b"observer-monitoring/helia integration test " + os.urandom(16)
        cid = await self.client.put(data)
        print("\n  put %d bytes -> %s" % (len(data), cid))
        assert is_valid_cid(cid)
        assert cid == raw_cid(data), "a one-chunk payload should get the raw-leaf CIDv1 computed locally"
        assert await self.client.get(cid) == data
        assert await self.client.put(data) == cid            # same bytes, same CID

    @tornado.testing.gen_test(timeout=30)
    async def test_binary_payload_near_the_limit(self):
        data = os.urandom(LIMIT - 1024)
        cid = await self.client.put(data)
        assert await self.client.get(cid) == data

    @tornado.testing.gen_test(timeout=30)
    async def test_unknown_cid_is_a_504_after_the_sidecars_own_timeout(self):
        missing = raw_cid(os.urandom(32))                      # well-formed, never stored
        start = time.monotonic()
        with pytest.raises(HeliaTimeout) as e:
            await self.client.get(missing)
        took = time.monotonic() - start
        print("\n  unknown CID -> %s (%d) after %.1fs" % (type(e.value).__name__, e.value.status, took))
        assert 4 <= took < 8

    @tornado.testing.gen_test(timeout=30)
    async def test_wrong_secret_is_misconfigured_502(self):
        wrong = HeliaClient(URL, "definitely-not-the-secret")
        try:
            with pytest.raises(HeliaMisconfigured) as e:
                await wrong.put(b"x")
            assert e.value.status == 502
            assert await wrong.health() is True               # /health is unauthenticated on the real sidecar
        finally:
            wrong.close()

    @tornado.testing.gen_test(timeout=30)
    async def test_unreachable_is_502(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        down = HeliaClient("http://127.0.0.1:%d" % port, SECRET, connect_timeout=1)
        try:
            with pytest.raises(HeliaUnavailable) as e:
                await down.put(b"x")
            assert e.value.status == 502 and await down.health() is False
        finally:
            down.close()

    @tornado.testing.gen_test(timeout=30)
    async def test_invalid_cid_client_side_and_sidecar_side(self):
        with pytest.raises(HeliaInvalidCid):
            await self.client.get("not-a-cid")
        status, body = raw_http("GET", "/retrieve/not-a-cid")
        print("\n  sidecar itself, GET /retrieve/not-a-cid -> %d %s" % (status, body.decode()))
        assert status == 400
        # shaped like a CID but not a decodable one: also a fast 400 from the sidecar, not a hang
        status, body = raw_http("GET", "/retrieve/b" + "a" * 30)
        print("  sidecar itself, shaped-but-undecodable CID -> %d %s" % (status, body.decode()))
        assert status == 400

    @tornado.testing.gen_test(timeout=30)
    async def test_oversized_upload_client_side_and_sidecar_side(self):
        with pytest.raises(HeliaPayloadTooLarge):
            await self.client.put(b"x" * (LIMIT + 1))
        status, body = raw_http("POST", "/upload", b"x" * (LIMIT + 1))
        print("\n  sidecar itself, %d-byte upload -> %d %s" % (LIMIT + 1, status, body.decode()))
        assert status == 413
        assert json.loads(body)["error"] == "payload_too_large"

    @tornado.testing.gen_test(timeout=30)
    async def test_sidecar_requires_the_secret_on_every_route_but_health(self):
        for method, path, body in (("POST", "/upload", b"x"), ("GET", "/list", None), ("GET", "/retrieve/" + raw_cid(b"x"), None)):
            status, _ = raw_http(method, path, body, token="")
            assert status == 401, (method, path)
        assert raw_http("GET", "/health", token="")[0] == 200


@pytest.mark.xfail(
    strict=False,
    reason=("upstream sidecar bug, fixed in Tornado-Backend-Stack#15 (a114973); fails against older sidecar builds: "
            "an upload with no Content-Type header is answered 200 with the EMPTY-file CID and nothing is stored. "
            "HeliaClient always sends application/octet-stream, so it is not affected. XPASSes against a114973 and later."))
def test_sidecar_should_not_silently_store_nothing_when_content_type_is_missing():
    # urllib adds a default Content-Type for a body, so go through http.client to really send none
    import http.client
    from urllib.parse import urlsplit
    u = urlsplit(URL)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=15)
    conn.putrequest("POST", "/upload", skip_accept_encoding=True)
    conn.putheader("X-Meshkit-Helia-Token", SECRET)
    conn.putheader("Content-Length", "11")
    conn.endheaders(b"hello world")
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    assert resp.status != 200 or json.loads(body)["cid"] == raw_cid(b"hello world")
