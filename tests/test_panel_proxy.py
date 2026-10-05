"""Reverse proxy and Cloudflare Access tests. Run: python -m pytest tests/test_panel_proxy.py"""
import re
import shutil
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from panel import auth, create_app  # noqa: E402
from panel.cfaccess import HEADER, AccessVerifier  # noqa: E402
from panel.db import connect  # noqa: E402

PW = "correct-horse-battery"
TEAM, AUD = "acme", "aud-tag-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key=KEY, alg="RS256", **over):
    now = int(time.time())
    claims = {"iss": f"https://{TEAM}.cloudflareaccess.com", "aud": [AUD], "iat": now, "exp": now + 300,
              "email": "alice@example.org"} | over
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm=alg)


def make_app(tmp_path, **cfg):
    inv = tmp_path / "inv"
    shutil.copytree(ROOT / "inventories" / "example", inv)
    app = create_app({"TESTING": True, "SECRET_KEY": "t", "INVENTORY": inv, "DB_PATH": tmp_path / "p.db",
                      "SNAPSHOT_DIR": tmp_path / "s", "JOB_LOG_DIR": tmp_path / "j", **cfg})
    db = connect(app.config["DB_PATH"])
    db.execute("INSERT INTO users (username, pw_hash, role, created) VALUES ('alice', ?, 'admin', ?)",
               (auth.hash_password(PW), time.time()))
    db.commit()
    db.close()
    return app


@pytest.fixture()
def gated(tmp_path):
    app = make_app(tmp_path, CF_ACCESS_TEAM=TEAM, CF_ACCESS_AUD=AUD)
    app.extensions["cf_access"].key_resolver = lambda _t: KEY.public_key()  # no network in tests
    return app


def test_access_required_for_every_page(gated):
    c = gated.test_client()
    assert c.get("/login").status_code == 403
    assert c.get("/static/app.css").status_code == 403
    assert c.post("/login", data={"username": "alice", "password": PW}).status_code == 403
    assert c.get("/healthz").status_code == 200  # local health check stays open


def test_valid_access_token_reaches_login(gated):
    r = gated.test_client().get("/login", headers={HEADER: token()})
    assert r.status_code == 200


@pytest.mark.parametrize("bad", [
    lambda: token(key=OTHER_KEY),                                    # signed by someone else
    lambda: token(aud=["other-app"]),                                # another Access application
    lambda: token(iss="https://evil.cloudflareaccess.com"),          # another team
    lambda: token(exp=int(time.time()) - 10),                        # expired
    lambda: token(exp=None),                                         # no expiry
    lambda: jwt.encode({"iss": f"https://{TEAM}.cloudflareaccess.com", "aud": AUD, "iat": 1, "exp": 4102444800},
                       "secret-of-at-least-32-bytes-for-hs256!", algorithm="HS256"),  # algorithm confusion
    lambda: "not.a.jwt",
])
def test_bad_tokens_rejected(gated, bad):
    assert gated.test_client().get("/login", headers={HEADER: bad()}).status_code == 403


def test_key_fetch_failure_fails_closed(gated):
    def boom(_t):
        raise OSError("certs endpoint unreachable")
    gated.extensions["cf_access"].key_resolver = boom
    assert gated.test_client().get("/login", headers={HEADER: token()}).status_code == 403


def test_access_email_recorded_in_audit(gated):
    c = gated.test_client()
    h = {HEADER: token(email="bob@example.org")}
    c.get("/login", headers=h)
    with c.session_transaction() as s:
        tok = s.get("csrf") or "x"
    html = c.get("/login", headers=h).get_data(as_text=True)
    tok = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    c.post("/login", data={"username": "alice", "password": PW, "csrf_token": tok}, headers=h)
    row = connect(gated.config["DB_PATH"]).execute("SELECT detail FROM audit WHERE action='login'").fetchone()
    assert "bob@example.org" in row["detail"]


def test_half_configured_access_refuses_to_start(tmp_path):
    with pytest.raises(RuntimeError, match="both"):
        make_app(tmp_path, CF_ACCESS_TEAM=TEAM)


def test_verifier_default_resolver_is_built_without_network():
    assert AccessVerifier(TEAM, AUD).issuer == "https://acme.cloudflareaccess.com"


def _ip(app, remote, header):
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": remote},
                                  headers={"CF-Connecting-IP": header} if header else {}):
        return auth.client_ip()


def test_client_ip_ignores_header_unless_enabled(tmp_path):
    app = make_app(tmp_path)
    assert _ip(app, "127.0.0.1", "203.0.113.9") == "127.0.0.1"


def test_client_ip_uses_header_only_from_loopback(tmp_path):
    app = make_app(tmp_path, TRUST_CF_IP=True)
    assert _ip(app, "127.0.0.1", "203.0.113.9") == "203.0.113.9"
    assert _ip(app, "::1", "2001:db8::5") == "2001:db8::5"
    assert _ip(app, "192.168.1.50", "203.0.113.9") == "192.168.1.50"  # forged header from the LAN
    assert _ip(app, "127.0.0.1", "not-an-ip") == "127.0.0.1"
    assert _ip(app, "127.0.0.1", "") == "127.0.0.1"


def test_lockout_counts_real_clients_separately(tmp_path):
    app = make_app(tmp_path, TRUST_CF_IP=True)
    db = connect(app.config["DB_PATH"])
    for _ in range(auth.MAX_FAILURES * 4):  # one noisy visitor behind the tunnel
        db.execute("INSERT INTO login_attempts (username, ip, ts) VALUES ('x', '198.51.100.7', ?)", (time.time(),))
    db.commit()
    assert auth.locked_out(db, "alice", "198.51.100.7")
    assert not auth.locked_out(db, "alice", "203.0.113.9")  # everyone else is unaffected


def test_hsts_when_secure_cookie(tmp_path):
    app = make_app(tmp_path, SESSION_COOKIE_SECURE=True)
    assert "max-age" in app.test_client().get("/healthz").headers["Strict-Transport-Security"]
    assert "Strict-Transport-Security" not in make_app(tmp_path / "b").test_client().get("/healthz").headers


def test_trusted_hosts(tmp_path):
    app = make_app(tmp_path, TRUSTED_HOSTS=["panel.example.org"])
    c = app.test_client()
    assert c.get("/healthz", headers={"Host": "panel.example.org"}).status_code == 200
    assert c.get("/healthz", headers={"Host": "evil.example"}).status_code == 400
