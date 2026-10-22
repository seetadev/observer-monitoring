import json
import pathlib
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import auth
import config
import root

ROOT = pathlib.Path(__file__).resolve().parent.parent
PASSWORD_WRONG = "wrong password value"


def request(peer, forwarded=None):
    headers = {} if forwarded is None else {"X-Forwarded-For": forwarded}
    return SimpleNamespace(remote_ip=peer, headers=headers)


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["10.0.0.5", "192.168.0.0/16"])


# --- client_address -----------------------------------------------------------------------------------------------

def test_forwarded_header_is_ignored_without_trusted_proxies():
    assert auth.client_address(request("198.51.100.7", "203.0.113.9")) == "198.51.100.7"


def test_forwarded_header_from_an_untrusted_peer_is_ignored(trusted):
    assert auth.client_address(request("198.51.100.7", "203.0.113.9")) == "198.51.100.7"


def test_trusted_proxy_vouches_for_the_client(trusted):
    assert auth.client_address(request("10.0.0.5", "203.0.113.9")) == "203.0.113.9"


def test_the_chain_is_read_from_the_right(trusted):
    # a client can prepend anything; only the entry added by our own proxy chain counts
    assert auth.client_address(request("10.0.0.5", "1.2.3.4, 203.0.113.9")) == "203.0.113.9"
    assert auth.client_address(request("10.0.0.5", "1.2.3.4, 203.0.113.9, 192.168.4.4")) == "203.0.113.9"


def test_networks_and_ipv6(trusted, monkeypatch):
    assert auth.client_address(request("192.168.7.7", "203.0.113.9")) == "203.0.113.9"
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["::1", "fd00::/8"])
    assert auth.client_address(request("::1", "2001:db8::5")) == "2001:db8::5"


def test_missing_empty_or_malformed_header_falls_back_to_the_peer(trusted):
    assert auth.client_address(request("10.0.0.5")) == "10.0.0.5"
    assert auth.client_address(request("10.0.0.5", "")) == "10.0.0.5"
    assert auth.client_address(request("10.0.0.5", "not-an-ip")) == "10.0.0.5"
    assert auth.client_address(request("10.0.0.5", "203.0.113.9, garbage")) == "10.0.0.5"


def test_chain_made_only_of_trusted_proxies_falls_back_to_the_peer(trusted):
    assert auth.client_address(request("10.0.0.5", "192.168.1.1, 10.0.0.5")) == "10.0.0.5"


def test_bad_trusted_proxy_setting_refuses_to_start(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["10.0.0.5", "proxy.example.org"])
    with pytest.raises(auth.ConfigError, match="OBSERVER_TRUSTED_PROXIES.*proxy.example.org"):
        auth.validate_config()


# --- the login limiter uses it ---------------------------------------------------------------------------------------

async def attempt(client, forwarded=None, password=PASSWORD_WRONG):
    headers = {} if forwarded is None else {"X-Forwarded-For": forwarded}
    return await client.post("/api", data={"action": "LOGIN_ADMIN", "password": password}, headers=headers)


async def test_spoofed_header_does_not_dodge_the_limit_when_no_proxy_is_trusted(anon_dash):
    codes = [(await attempt(anon_dash, "203.0.113.%d" % i)).status_code for i in range(1, 8)]
    assert codes == [401] * 5 + [429] * 2


async def test_clients_behind_a_trusted_proxy_are_limited_separately(anon_dash, monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["127.0.0.1"])      # the test client connects from loopback
    for _ in range(5):
        assert (await attempt(anon_dash, "203.0.113.1")).status_code == 401
    assert (await attempt(anon_dash, "203.0.113.1")).status_code == 429
    assert (await attempt(anon_dash, "203.0.113.2")).status_code == 401      # another user is not locked out


async def test_spoofed_prefix_behind_a_trusted_proxy_cannot_reset_the_limit(anon_dash, monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["127.0.0.1"])
    codes = [(await attempt(anon_dash, "198.18.0.%d, 203.0.113.1" % i)).status_code for i in range(1, 8)]
    assert codes == [401] * 5 + [429] * 2


# --- front-end endpoints ---------------------------------------------------------------------------------------------

NODE = shutil.which("node")


def endpoints(location):
    script = (ROOT / "static" / "js" / "endpoints.js").read_text() + \
        "\nconsole.log(JSON.stringify(observerEndpoints(%s)));" % json.dumps(location)
    return json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True).stdout)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_direct_layout_uses_the_service_ports_with_the_page_scheme():
    result = endpoints({"port": "9000", "protocol": "http:", "hostname": "monitor.lan",
                        "origin": "http://monitor.lan:9000"})
    assert result == {"api": "http://monitor.lan:9000/api", "live": "http://monitor.lan:9002/live",
                      "compute": "http://monitor.lan:9003/compute", "files": "http://monitor.lan:9000/files"}


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize("port", ["", "8443"])
def test_proxied_layout_is_same_origin_and_never_downgrades_to_http(port):
    origin = "https://observer.example.org" + (":" + port if port else "")
    result = endpoints({"port": port, "protocol": "https:", "hostname": "observer.example.org", "origin": origin})
    assert result == {"api": origin + "/api", "live": origin + "/_live", "compute": origin + "/_compute",
                      "files": origin + "/files"}


def test_pages_load_the_endpoint_helper_before_the_shared_script():
    head = (ROOT / "templates" / "headTemplate.html").read_text()
    assert head.index("js/endpoints.js") < head.index("js/shared-scripts.js")
    assert "http://" not in (ROOT / "static" / "js" / "shared-scripts.js").read_text().split("function pageInit")[0]


# --- nginx and compose files -----------------------------------------------------------------------------------------

CONF = (ROOT / "deploy" / "nginx" / "nginx.conf").read_text()


def test_nginx_config_terminates_tls_safely_and_routes_the_services():
    for needle in ("ssl_protocols TLSv1.2 TLSv1.3", "ssl_session_tickets off", "Strict-Transport-Security",
                   "server_tokens off", "proxy_set_header X-Forwarded-For $remote_addr;",
                   "location = /_live", "http://live:9002/live", "location = /_compute", "http://compute:9003/compute",
                   "proxy_pass http://dash:9000", "listen 8890 ssl", "proxy_pass listener:8889"):
        assert needle in CONF
    assert "$proxy_add_x_forwarded_for" not in CONF      # the client's own header must never be forwarded
    assert "ssl_verify_client off" not in CONF and "TLSv1 " not in CONF and "SSLv3" not in CONF


def test_certificates_are_never_committed():
    ignore = (ROOT / "deploy" / "tls" / ".gitignore").read_text()
    assert "*.pem" in ignore and "*.key" in ignore
    tracked = subprocess.run(["git", "ls-files", "deploy/tls"], cwd=ROOT, capture_output=True, text=True).stdout
    assert ".pem" not in tracked and ".key" not in tracked


DOCKER = shutil.which("docker")


@pytest.mark.skipif(DOCKER is None, reason="docker not available")
def test_compose_file_is_valid_with_and_without_the_tls_profile():
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "OBSERVER_COOKIE_SECRET": "x", "OBSERVER_ADMIN_PASSWORD_HASH": "x"}
    for extra in ([], ["--profile", "tls"]):
        subprocess.run([DOCKER, "compose", *extra, "config", "-q"], cwd=ROOT, env=env, check=True,
                       capture_output=True)
    out = subprocess.run([DOCKER, "compose", "--profile", "tls", "config"], cwd=ROOT, env=env, check=True,
                         capture_output=True, text=True).stdout
    assert "172.29.250.2" in out
    plain = subprocess.run([DOCKER, "compose", "config", "--services"], cwd=ROOT, env=env, check=True,
                           capture_output=True, text=True).stdout.split()
    assert "proxy" not in plain      # off unless the profile is selected


@pytest.mark.skipif(DOCKER is None or shutil.which("openssl") is None, reason="docker/openssl not available")
def test_nginx_accepts_the_configuration(tmp_path):
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout",
                    str(tmp_path / "privkey.pem"), "-out", str(tmp_path / "fullchain.pem"), "-subj", "/CN=localhost"],
                   check=True, capture_output=True)
    (tmp_path / "privkey.pem").chmod(0o644)
    result = subprocess.run(
        [DOCKER, "run", "--rm", "--add-host", "dash:127.0.0.1", "--add-host", "live:127.0.0.1",
         "--add-host", "compute:127.0.0.1", "--add-host", "listener:127.0.0.1",
         "-v", "%s:/etc/nginx/nginx.conf:ro" % (ROOT / "deploy" / "nginx" / "nginx.conf"),
         "-v", "%s:/etc/nginx/tls:ro" % tmp_path, "nginx:1.27-alpine", "nginx", "-t"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "syntax is ok" in result.stderr


def test_root_still_uses_the_shared_helper_for_the_limiter():
    assert "auth.client_address(self.request)" in pathlib.Path(root.__file__).read_text()
