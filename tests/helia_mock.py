"""MOCK of the Helia sidecar's HTTP contract (meshkit-sidecar-helia/index.js). NOT the real sidecar and not real IPFS:
CIDs here are computed locally as CIDv1/raw/sha2-256 (what a one-chunk payload gets), nothing more. The opt-in integration test
(tests/integration/test_helia_real.py) runs against the real sidecar.

Behaviour copied from the real sidecar: every route but /health needs X-Meshkit-Helia-Token (else 401); POST /upload
takes raw bytes, answers {"cid"} and 413 over the limit; GET /retrieve/<cid> answers 400 for a malformed CID and, for
a CID it does not hold, 504 (the real sidecar's default offline mode waits 5 s and then answers 504).
"""
import asyncio
import base64
import hashlib
import json
import re

import tornado.web

_CID = re.compile(r"Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{20,}")


def fake_cid(data):
    """CIDv1, raw codec, sha2-256: the CID IPFS gives a payload that fits in one chunk (the integration test checks
    the real sidecar returns exactly this for small payloads)."""
    multihash = bytes([0x01, 0x55, 0x12, 0x20]) + hashlib.sha256(data).digest()
    return "b" + base64.b32encode(multihash).decode().lower().rstrip("=")


class Mock:
    def __init__(self, secret="test-secret-not-real", max_upload=1024):
        self.secret = secret
        self.max_upload = max_upload
        self.store = {}
        self.requests = []        # (method, path, token header or None)
        self.mode = None          # None | 502 | 503 | 504 | "hang" | 302 | "bad-json" | "bad-cid"

    def app(self):
        return tornado.web.Application([
            (r"/health", _Health, dict(m=self)),
            (r"/upload", _Upload, dict(m=self)),
            (r"/retrieve/(.*)", _Retrieve, dict(m=self)),
            (r"/list", _List, dict(m=self)),
            (r"/elsewhere", _Elsewhere, dict(m=self)),                   # a redirect target that records who arrives
            (r"/meshkit-helia/upload", _Upload, dict(m=self)),          # the Tornado proxy's paths
            (r"/meshkit-helia/retrieve/(.*)", _Retrieve, dict(m=self)),
        ])


class _Base(tornado.web.RequestHandler):
    needs_token = True

    def initialize(self, m):
        self.m = m

    def prepare(self):
        token = self.request.headers.get("X-Meshkit-Helia-Token")
        self.m.requests.append((self.request.method, self.request.path, token))
        if self.needs_token and token != self.m.secret:
            self.set_status(401)
            self.finish({"error": "unauthorized", "message": "missing or invalid X-Meshkit-Helia-Token"})
            return
        if isinstance(self.m.mode, int) and self.m.mode != 302:
            self.set_status(self.m.mode)
            self.finish({"error": "forced %d" % self.m.mode})
            return
        if self.m.mode == 302:
            self.redirect("/elsewhere")
            return

    async def maybe_hang(self):
        if self.m.mode == "hang":
            await asyncio.sleep(3)


class _Health(_Base):
    needs_token = False

    async def get(self):
        await self.maybe_hang()
        self.write({"status": "ok", "peerId": "12D3KooWMockMockMockMockMockMockMockMockMockMock"})


class _Upload(_Base):
    async def post(self):
        await self.maybe_hang()
        body = self.request.body
        if len(body) > self.m.max_upload:
            self.set_status(413)
            return self.write({"error": "payload_too_large", "message": "upload exceeds the limit"})
        cid = fake_cid(body)
        self.m.store[cid] = body
        if self.m.mode == "bad-json":
            return self.write("<html>not json</html>")
        if self.m.mode == "bad-cid":
            return self.write({"cid": "../../etc/passwd"})
        self.write({"cid": cid})


class _Retrieve(_Base):
    async def get(self, cid):
        await self.maybe_hang()
        if not _CID.fullmatch(cid):
            self.set_status(400)
            return self.write({"error": "invalid CID"})
        if cid not in self.m.store:
            self.set_status(504)       # offline mode: an unknown CID times out
            return self.write({"error": "retrieve timed out after 5s"})
        self.set_header("Content-Type", "application/octet-stream")
        self.write(self.m.store[cid])


class _List(_Base):
    def get(self):
        self.write(json.dumps([{"cid": c} for c in self.m.store]))


class _Elsewhere(_Base):
    needs_token = False

    def get(self):
        self.write({"cid": fake_cid(b"redirected")})

    post = get
