"""Unit tests for integrations/helia.py against tests/helia_mock.py (a mock of the sidecar's HTTP contract)."""
import socket

import pytest
import tornado.testing
import tornado.web

from integrations.helia import (
    HeliaClient, HeliaError, HeliaInvalidCid, HeliaMisconfigured, HeliaNotFound, HeliaPayloadTooLarge,
    HeliaTimeout, HeliaUnavailable, is_valid_cid,
)
from tests.helia_mock import Mock, fake_cid

SECRET = "test-secret-not-real"
GOOD_CID = fake_cid(b"anything")


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ClientTest(tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        self.mock = Mock(secret=SECRET, max_upload=1024)
        super().setUp()
        self.client = HeliaClient("http://127.0.0.1:%d" % self.get_http_port(), SECRET, max_upload_bytes=1024,
                                  connect_timeout=1, request_timeout=0.5)

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def get_app(self):
        return self.mock.app()

    # ---- success -------------------------------------------------------------------------------------------
    @tornado.testing.gen_test
    async def test_put_returns_a_cid_and_get_returns_the_same_bytes(self):
        cid = await self.client.put(b"hello helia")
        assert is_valid_cid(cid) and cid == fake_cid(b"hello helia")
        assert await self.client.get(cid) == b"hello helia"

    @tornado.testing.gen_test
    async def test_every_call_but_health_sends_the_secret_and_health_does_not_need_it(self):
        cid = await self.client.put(b"x")
        await self.client.get(cid)
        assert await self.client.health() is True
        sent = {(m, p): t for m, p, t in self.mock.requests}
        assert sent[("POST", "/upload")] == SECRET
        assert sent[("GET", "/retrieve/" + cid)] == SECRET

    @tornado.testing.gen_test
    async def test_health_is_true_without_a_secret_like_the_real_sidecar(self):
        anonymous = HeliaClient("http://127.0.0.1:%d" % self.get_http_port())
        try:
            assert await anonymous.health() is True
        finally:
            anonymous.close()

    @tornado.testing.gen_test
    async def test_binary_payload_round_trips_unchanged(self):
        data = bytes(range(256)) * 3
        assert await self.client.get(await self.client.put(data)) == data

    @tornado.testing.gen_test
    async def test_prefix_is_used_for_the_tornado_proxy_paths(self):
        proxied = HeliaClient("http://127.0.0.1:%d/" % self.get_http_port(), SECRET, prefix="/meshkit-helia")
        try:
            cid = await proxied.put(b"via proxy")
            assert await proxied.get(cid) == b"via proxy"
            assert ("POST", "/meshkit-helia/upload", SECRET) in self.mock.requests
        finally:
            proxied.close()

    # ---- 502: unreachable / unavailable -------------------------------------------------------------------
    @tornado.testing.gen_test
    async def test_connection_refused_is_unavailable_502(self):
        down = HeliaClient("http://127.0.0.1:%d" % closed_port(), SECRET, connect_timeout=1)
        try:
            with pytest.raises(HeliaUnavailable) as e:
                await down.put(b"x")
            assert e.value.status == 502
            with pytest.raises(HeliaUnavailable):
                await down.get(GOOD_CID)
            assert await down.health() is False
        finally:
            down.close()

    @tornado.testing.gen_test
    async def test_sidecar_502_and_503_are_unavailable(self):
        for status in (502, 503):
            self.mock.mode = status
            with pytest.raises(HeliaUnavailable) as e:
                await self.client.put(b"x")
            assert e.value.status == 502
            with pytest.raises(HeliaUnavailable):
                await self.client.get(GOOD_CID)
            assert await self.client.health() is False

    @tornado.testing.gen_test
    async def test_wrong_secret_is_a_misconfiguration_reported_as_502(self):
        wrong = HeliaClient("http://127.0.0.1:%d" % self.get_http_port(), "not-the-secret")
        try:
            with pytest.raises(HeliaMisconfigured) as e:
                await wrong.put(b"x")
            assert e.value.status == 502 and isinstance(e.value, HeliaUnavailable)
            assert "not-the-secret" not in str(e.value) and "not-the-secret" not in repr(wrong)
        finally:
            wrong.close()

    # ---- 504: slow / timed out ----------------------------------------------------------------------------
    @tornado.testing.gen_test
    async def test_sidecar_504_is_timeout(self):
        self.mock.mode = 504
        with pytest.raises(HeliaTimeout) as e:
            await self.client.get(GOOD_CID)
        assert e.value.status == 504

    @tornado.testing.gen_test
    async def test_unknown_cid_is_timeout_504_like_the_real_offline_sidecar(self):
        with pytest.raises(HeliaTimeout):
            await self.client.get(GOOD_CID)

    @tornado.testing.gen_test
    async def test_a_hung_sidecar_times_out_fast_as_504(self):
        self.mock.mode = "hang"
        with pytest.raises(HeliaTimeout):
            await self.client.put(b"x")
        with pytest.raises(HeliaTimeout):
            await self.client.get(GOOD_CID)
        assert await self.client.health() is False

    # ---- invalid CID --------------------------------------------------------------------------------------
    @tornado.testing.gen_test
    async def test_invalid_cids_are_refused_locally_without_calling_the_sidecar(self):
        bad = ["", "Qm", "../../etc/passwd", "bafy/../x", GOOD_CID + "\n", GOOD_CID.upper(), "Qm" + "0" * 44,
               "b" + "1" * 30, " " + GOOD_CID, GOOD_CID + "?x=1", "http://evil.invalid/", None, 5, b"bafy"]
        for value in bad:
            with pytest.raises(HeliaInvalidCid) as e:
                await self.client.get(value)
            assert e.value.status == 400, value
        assert self.mock.requests == []

    @tornado.testing.gen_test
    async def test_sidecar_400_is_invalid_cid_too(self):
        self.mock.mode = 400
        with pytest.raises(HeliaInvalidCid):
            await self.client.get(GOOD_CID)

    # ---- oversize -----------------------------------------------------------------------------------------
    @tornado.testing.gen_test
    async def test_oversized_upload_is_refused_locally_413(self):
        with pytest.raises(HeliaPayloadTooLarge) as e:
            await self.client.put(b"x" * 1025)
        assert e.value.status == 413
        assert self.mock.requests == []
        assert await self.client.put(b"x" * 1024)          # exactly the limit is fine

    @tornado.testing.gen_test
    async def test_sidecar_413_is_payload_too_large(self):
        bigger = HeliaClient("http://127.0.0.1:%d" % self.get_http_port(), SECRET, max_upload_bytes=10_000)
        try:
            with pytest.raises(HeliaPayloadTooLarge):
                await bigger.put(b"x" * 2000)              # the sidecar's own limit (1024) is hit
        finally:
            bigger.close()

    @tornado.testing.gen_test
    async def test_empty_and_non_bytes_payloads_are_refused(self):
        with pytest.raises(ValueError):
            await self.client.put(b"")
        for bad in ("text", 5, None, ["x"]):
            with pytest.raises(TypeError):
                await self.client.put(bad)
        assert await self.client.put(bytearray(b"ok")) and await self.client.put(memoryview(b"ok2"))

    @tornado.testing.gen_test
    async def test_download_over_the_limit_fails_instead_of_buffering(self):
        cid = await self.client.put(b"y" * 1024)
        small = HeliaClient("http://127.0.0.1:%d" % self.get_http_port(), SECRET, max_download_bytes=100)
        try:
            with pytest.raises(HeliaUnavailable):          # Tornado reports an oversized body as a closed connection
                await small.get(cid)
        finally:
            small.close()

    # ---- other failure shapes -----------------------------------------------------------------------------
    @tornado.testing.gen_test
    async def test_sidecar_404_is_not_found(self):
        self.mock.mode = 404
        with pytest.raises(HeliaNotFound) as e:
            await self.client.get(GOOD_CID)
        assert e.value.status == 404

    @tornado.testing.gen_test
    async def test_unexpected_status_is_a_plain_error(self):
        self.mock.mode = 500
        with pytest.raises(HeliaError) as e:
            await self.client.put(b"x")
        assert type(e.value) is HeliaError and e.value.status == 500

    @tornado.testing.gen_test
    async def test_malformed_upload_replies_are_rejected(self):
        for mode in ("bad-json", "bad-cid"):
            self.mock.mode = mode
            with pytest.raises(HeliaError):
                await self.client.put(b"x")

    @tornado.testing.gen_test
    async def test_redirects_are_never_followed(self):
        self.mock.mode = 302
        with pytest.raises(HeliaError):
            await self.client.put(b"x")
        assert [r[1] for r in self.mock.requests] == ["/upload"]     # never asked /elsewhere with the secret


class TestConfig:
    def test_unset_url_means_disabled(self):
        assert HeliaClient.from_env({}) is None
        assert HeliaClient.from_env({"HELIA_SIDECAR_URL": "  "}) is None

    def test_from_env_reads_every_setting(self):
        c = HeliaClient.from_env({"HELIA_SIDECAR_URL": "http://sidecar:5052/", "HELIA_SIDECAR_SECRET": "s",
                                  "HELIA_API_PREFIX": "/meshkit-helia/", "HELIA_HEALTH_PATH": "/h",
                                  "HELIA_MAX_UPLOAD_MB": "0.5", "HELIA_CONNECT_TIMEOUT": "2", "HELIA_REQUEST_TIMEOUT": "4"})
        try:
            assert (c.base_url, c.prefix, c.health_path) == ("http://sidecar:5052", "/meshkit-helia", "/h")
            assert (c.max_upload_bytes, c.connect_timeout, c.request_timeout) == (524288, 2.0, 4.0)
            assert "s'" not in repr(c) and "secret=set" in repr(c)
        finally:
            c.close()

    def test_bad_values_fail_loudly(self):
        for env in ({"HELIA_SIDECAR_URL": "ftp://x"}, {"HELIA_SIDECAR_URL": "not a url"},
                    {"HELIA_SIDECAR_URL": "http://user:pw@host:1"}, {"HELIA_SIDECAR_URL": "http://h:1/?a=1"},
                    {"HELIA_SIDECAR_URL": "http://h:1", "HELIA_API_PREFIX": "no-slash"},
                    {"HELIA_SIDECAR_URL": "http://h:1", "HELIA_API_PREFIX": "/a b"},
                    {"HELIA_SIDECAR_URL": "http://h:1", "HELIA_API_PREFIX": "//evil.invalid"},
                    {"HELIA_SIDECAR_URL": "http://h:1", "HELIA_API_PREFIX": "/../x"},
                    {"HELIA_SIDECAR_URL": "http://h:1", "HELIA_HEALTH_PATH": "/a/./b"},
                    {"HELIA_SIDECAR_URL": "http://h:1", "HELIA_MAX_UPLOAD_MB": "lots"},
                    {"HELIA_SIDECAR_URL": "http://h:1", "HELIA_REQUEST_TIMEOUT": "0"}):
            with pytest.raises(ValueError):
                HeliaClient.from_env(env)


def test_the_module_is_not_wired_into_the_app():
    """Scaffold only: nothing in the app imports it yet."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for path in [root / "run.py", *(root / "controllers").glob("*.py"), *(root / "modules").glob("*.py")]:
        assert "helia" not in path.read_text().lower(), path
