"""Authentication and authorization with TRACKER_AUTH=on (SYNTHETIC users; no real credentials anywhere)."""

import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from tracker import auth
from tracker.db import AuditEvent, User, UserSession
from tracker.main import create_app
from tracker.users import parser, run

from .conftest import WALLET, database_url
from .test_api import SIG, SyntheticPrices, SyntheticQuotes, SyntheticRPC

PASSWORD = "synthetic-pass-123"


def add_user(c, username, role, password=PASSWORD):
    with c.app.state.sessions() as s:
        s.add(User(username=username, password_hash=auth.hash_password(password), role=role))
        s.commit()


def login(c, username, password=PASSWORD):
    return c.post("/api/auth/login", json={"username": username, "password": password})


def audit_actions(c):
    with c.app.state.sessions() as s:
        return [(e.username, e.action, e.status) for e in s.scalars(select(AuditEvent).order_by(AuditEvent.id))]


@pytest.fixture
def secured(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_AUTH", "on")
    monkeypatch.setenv("TRACKER_COOKIE_SECURE", "0")  # the test client speaks plain HTTP
    app = create_app(database_url(tmp_path, "auth.db"), rpc=SyntheticRPC(), prices=SyntheticPrices(),
                     quotes=SyntheticQuotes())
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()


def test_auth_is_off_by_default_and_says_so(tmp_path):
    app = create_app(database_url(tmp_path, "open.db"), rpc=SyntheticRPC(), prices=SyntheticPrices(),
                     quotes=SyntheticQuotes())
    with TestClient(app) as c:
        assert c.get("/api/auth/status").json()["mode"] == "off"
        assert c.get("/api/wallets").status_code == 200
        assert login(c, "anyone").status_code == 400
    app.state.engine.dispose()


def test_everything_but_health_status_login_and_hooks_needs_a_session(secured):
    c = secured
    assert c.get("/api/wallets").status_code == 401 and c.get("/api/wallets").json()["auth"] == "required"
    assert c.get("/api/health").status_code == 200
    status = c.get("/api/auth/status").json()
    assert status["mode"] == "on" and not status["authenticated"] and status["setup_required"]
    assert c.post("/api/wallets", json={"address": WALLET}).status_code == 401
    assert c.get("/api/users").status_code == 401


def test_sign_in_sets_a_strict_http_only_cookie_and_sign_out_ends_the_session(secured, monkeypatch):
    c = secured
    add_user(c, "ana", "analyst")
    wrong = login(c, "ana", "not-the-password")
    assert wrong.status_code == 401 and "incorrectos" in wrong.json()["detail"]
    assert login(c, "nobody").json()["detail"] == wrong.json()["detail"]  # no account enumeration
    ok = login(c, "ana")
    assert ok.status_code == 200 and ok.json()["user"] == {"username": "ana", "role": "analyst"}
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=604800" in cookie
    assert c.get("/api/wallets").status_code == 200
    assert c.get("/api/auth/status").json()["user"] == {"username": "ana", "role": "analyst", "via": "session"}
    with c.app.state.sessions() as s:
        stored = s.scalar(select(UserSession.token_hash))
    assert stored != c.cookies.get(auth.SESSION_COOKIE) and len(stored) == 64  # only the hash is stored
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/wallets").status_code == 401
    monkeypatch.setenv("TRACKER_COOKIE_SECURE", "1")
    assert "secure" in login(c, "ana").headers["set-cookie"].lower()  # the default outside tests
    assert [a for _, a, _ in audit_actions(c)][:4] == ["login_failed", "login_failed", "login", "logout"]


def test_five_failures_lock_the_account_for_a_while(secured):
    c = secured
    add_user(c, "ana", "analyst")
    for _ in range(auth.LOCK_AFTER):
        assert login(c, "ana", "wrong-password-xx").status_code == 401
    assert login(c, "ana").status_code == 401  # locked, even with the right password
    with c.app.state.sessions() as s:
        s.execute(update(User).values(locked_until=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
        s.commit()
    assert login(c, "ana").status_code == 200


def test_roles_viewer_reads_analyst_operates_admin_manages(secured):
    c = secured
    for name, role in (("vera", "viewer"), ("ana", "analyst"), ("adam", "admin")):
        add_user(c, name, role)
    login(c, "vera")
    assert c.get("/api/wallets").status_code == 200
    denied = c.post("/api/wallets", json={"address": WALLET})
    assert denied.status_code == 403 and "lector" in denied.json()["detail"]
    c.post("/api/auth/logout")
    login(c, "ana")
    assert c.post("/api/wallets", json={"address": WALLET}).status_code == 201
    assert c.get("/api/users").status_code == 403 and c.get("/api/audit").status_code == 403
    c.post("/api/auth/logout")
    login(c, "adam")
    assert {u["username"] for u in c.get("/api/users").json()["items"]} == {"vera", "ana", "adam"}
    assert ("ana", "POST /api/wallets", 201) in audit_actions(c)
    assert c.get("/api/audit").json()["items"][0]["action"] == "login"


def test_personal_tokens_carry_the_owner_role_and_can_be_revoked(secured):
    c = secured
    add_user(c, "vera", "viewer")
    login(c, "vera")
    created = c.post("/api/auth/tokens", json={"name": "script"}).json()
    assert created["token"].startswith("trk_") and created["name"] == "script"
    assert "token" not in c.get("/api/auth/tokens").json()["items"][0]  # shown once
    c.post("/api/auth/logout")
    bearer = {"Authorization": f"Bearer {created['token']}"}
    assert c.get("/api/wallets", headers=bearer).status_code == 200
    assert c.post("/api/wallets", json={"address": WALLET}, headers=bearer).status_code == 403
    assert c.get("/api/auth/status", headers=bearer).json()["user"]["via"] == "token"
    assert c.post(f"/api/auth/tokens/{created['id']}/revoke", headers=bearer).json()["revoked"]
    assert c.get("/api/wallets", headers=bearer).status_code == 401
    assert c.get("/api/wallets", headers={"Authorization": "Bearer trk_forged"}).status_code == 401


def test_admins_create_change_and_disable_users_and_keep_one_admin(secured):
    c = secured
    add_user(c, "adam", "admin")
    login(c, "adam")
    assert c.post("/api/users", json={"username": "ana", "password": "short", "role": "analyst"}).status_code == 422
    assert c.post("/api/users", json={"username": "ana", "password": "ana-is-in-it-123"}).status_code == 422
    created = c.post("/api/users", json={"username": "ana", "password": PASSWORD, "role": "analyst"}).json()
    assert created["role"] == "analyst" and "password_hash" not in created
    assert c.post("/api/users", json={"username": "ana", "password": PASSWORD}).status_code == 409
    adam_id = next(u["id"] for u in c.get("/api/users").json()["items"] if u["username"] == "adam")
    assert c.patch(f"/api/users/{adam_id}", json={"role": "viewer"}).status_code == 409  # the last admin
    other = TestClient(c.app)
    login(other, "ana")
    token = other.post("/api/auth/tokens", json={"name": "t"}).json()["token"]
    assert c.patch(f"/api/users/{created['id']}", json={"role": "viewer"}).json()["role"] == "viewer"
    assert other.get("/api/wallets").status_code == 401  # a role change ends the user's sessions
    login(other, "ana")
    assert other.post("/api/wallets", json={"address": WALLET}).status_code == 403
    assert c.patch(f"/api/users/{created['id']}", json={"active": False}).json()["active"] is False
    assert login(other, "ana").status_code == 401
    assert other.get("/api/wallets", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_changing_the_password_ends_the_other_sessions(secured):
    c = secured
    add_user(c, "ana", "analyst")
    other = TestClient(c.app)
    login(c, "ana")
    login(other, "ana")
    assert c.post("/api/auth/password", json={"current_password": "nope", "new_password": "x" * 20}).status_code == 401
    assert c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "short"}).status_code == 422
    changed = c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "a-new-long-pass-1"})
    assert changed.json() == {"ok": True, "sessions_ended": 1}
    assert c.get("/api/wallets").status_code == 200 and other.get("/api/wallets").status_code == 401
    assert login(other, "ana", "a-new-long-pass-1").status_code == 200


def test_idle_and_expired_sessions_are_rejected(secured):
    c = secured
    add_user(c, "ana", "analyst")
    login(c, "ana")
    with c.app.state.sessions() as s:
        s.execute(update(UserSession).values(last_seen_at=(datetime.now(timezone.utc) - auth.SESSION_IDLE).isoformat()))
        s.commit()
    assert c.get("/api/wallets").status_code == 401
    login(c, "ana")
    with c.app.state.sessions() as s:
        s.execute(update(UserSession).values(expires_at=datetime.now(timezone.utc).isoformat()))
        s.commit()
    assert c.get("/api/wallets").status_code == 401


def test_the_provider_webhook_keeps_its_own_secret(secured, monkeypatch):
    c = secured
    monkeypatch.setenv("TRACKER_HOOK_SECRET", "synthetic-hook-secret")
    body = [{"signature": SIG}]
    assert c.post("/api/hooks/transactions", json=body).status_code == 401
    ok = c.post("/api/hooks/transactions", json=body, headers={"X-Tracker-Hook-Secret": "synthetic-hook-secret"})
    assert ok.status_code == 200 and ok.json()["processed"] == 1


def test_requests_and_sign_in_attempts_are_rate_limited(secured, monkeypatch):
    c = secured
    add_user(c, "ana", "analyst")
    login(c, "ana")
    monkeypatch.setenv("TRACKER_RATE_LIMIT_PER_MINUTE", "3")
    codes = [c.get("/api/wallets").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    limited = c.get("/api/wallets")
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    fresh = TestClient(c.app)
    attempts = [login(fresh, "ana", "wrong-password-xx").status_code for _ in range(auth.LOGIN_LIMIT_PER_MINUTE + 1)]
    assert attempts[-1] == 429


def test_forwarded_client_address_is_trusted_only_from_the_proxy(secured, monkeypatch):
    c = secured
    add_user(c, "ana", "analyst")
    login(c, "nobody")  # recorded with the direct client address, ignoring the header below
    monkeypatch.setenv("TRACKER_ACCESS_TOKEN", "synthetic-proxy-token")
    c.post("/api/auth/login", json={"username": "nobody", "password": "x"},
           headers={"X-Tracker-Token": "synthetic-proxy-token", "X-Forwarded-For": "203.0.113.7, 10.0.0.2"})
    with c.app.state.sessions() as s:
        ips = [e.ip for e in s.scalars(select(AuditEvent).order_by(AuditEvent.id))]
    assert ips[0] != "203.0.113.7" and ips[1] == "203.0.113.7"


def test_the_first_admin_is_created_from_the_command_line(secured, monkeypatch):
    c = secured
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    with c.app.state.sessions() as s:
        assert "creado" in run(s, parser().parse_args(["create", "root", "--password-stdin"]))
        assert "root\tadmin\tactivo" in run(s, parser().parse_args(["list"]))
        with pytest.raises(SystemExit):
            run(s, parser().parse_args(["disable", "root"]))  # the last admin
    assert login(c, "root").json()["user"]["role"] == "admin"
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with c.app.state.sessions() as s, pytest.raises(SystemExit):
        run(s, parser().parse_args(["create", "other", "--password-stdin"]))
