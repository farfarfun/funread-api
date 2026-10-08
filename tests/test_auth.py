"""Pre-shared password, signed session cookie, and what each guard lets through."""

import time

import pytest
from fastapi.testclient import TestClient

from funread_api.app import create_app
from funread_api.security import COOKIE_NAME, issue_token, verify_token


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolate the DB. Auth env is already neutralised by the autouse conftest fixture."""
    monkeypatch.setenv("FUNREAD_DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setenv("FUNREAD_CACHE_ROOT", str(tmp_path / "hubs"))
    return monkeypatch


# ------------------------------------------------------------------ token


def test_token_round_trips():
    token = issue_token("secret")

    assert verify_token(token, "secret") is True


def test_token_is_rejected_under_another_password():
    """Changing the password is the only session revocation there is."""
    token = issue_token("secret")

    assert verify_token(token, "other") is False


def test_expired_token_is_rejected():
    #  Minted far enough in the past that TTL has run out
    token = issue_token("secret", now=time.time() - 365 * 24 * 3600)

    assert verify_token(token, "secret") is False


@pytest.mark.parametrize("token", ["", "garbage", "notanint.abc", "123"])
def test_malformed_tokens_are_rejected(token):
    assert verify_token(token, "secret") is False


# ------------------------------------------------------------------ 未配置口令


def test_without_a_password_everything_stays_open(env):
    """Upgrading must not lock out the existing localhost single-user setup."""
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/sources").status_code == 200
        assert client.get("/api/v1/shelf").status_code == 200

        state = client.get("/api/v1/auth/me").json()

    assert state == {"auth_required": False, "authenticated": True, "reader_public": False}


def test_login_without_a_configured_password_is_a_no_op(env):
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/auth/login", json={"password": "whatever"})

    assert response.status_code == 200
    assert response.json()["auth_required"] is False
    assert COOKIE_NAME not in response.cookies


# ------------------------------------------------------------------ 配置了口令


def test_protected_endpoints_need_a_session(env):
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/sources").status_code == 401
        assert client.get("/api/v1/shelf").status_code == 401
        assert client.get("/api/v1/reader/search", params={"keyword": "剑"}).status_code == 401


def test_healthz_and_login_stay_reachable(env):
    """Both are needed before a session exists -- one for probes, one to get in."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/v1/auth/me").json()["authenticated"] is False


def test_login_then_use_then_logout(env):
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        assert client.post("/api/v1/auth/login", json={"password": "wrong"}).status_code == 401

        login = client.post("/api/v1/auth/login", json={"password": "hunter2"})
        assert login.status_code == 200
        assert login.json()["authenticated"] is True

        #  TestClient keeps the cookie jar, so this is the browser's view
        assert client.get("/api/v1/shelf").status_code == 200

        assert client.post("/api/v1/auth/logout").status_code == 204
        assert client.get("/api/v1/shelf").status_code == 401


def test_session_cookie_is_httponly(env):
    """It is only ever read by the server; JS access would just widen XSS."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        login = client.post("/api/v1/auth/login", json={"password": "hunter2"})

    assert "httponly" in login.headers["set-cookie"].lower()


def test_a_forged_cookie_does_not_get_in(env):
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        client.cookies.set(COOKIE_NAME, issue_token("hunter3"))
        assert client.get("/api/v1/shelf").status_code == 401


# ------------------------------------------------------------------ 公开阅读端


def test_reader_public_opens_read_only_gets(env):
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")
    env.setenv("FUNREAD_READER_PUBLIC", "1")

    with TestClient(create_app()) as client:
        #  No sources scanned, so this is an empty result rather than a 401
        assert client.get("/api/v1/reader/search", params={"keyword": "剑"}).status_code == 200


def test_reader_public_does_not_open_writes(env):
    """The flag is about letting people *read*, not letting them drive the box."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")
    env.setenv("FUNREAD_READER_PUBLIC", "1")

    with TestClient(create_app()) as client:
        assert client.post("/api/v1/reader/scan").status_code == 401
        assert client.get("/api/v1/shelf").status_code == 401
        assert client.post("/api/v1/shelf", json={"name": "剑来"}).status_code == 401


def test_reader_public_does_not_open_the_ssrf_endpoint(env):
    """POST /sources fetches an arbitrary URL server-side; it stays locked."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")
    env.setenv("FUNREAD_READER_PUBLIC", "1")

    with TestClient(create_app()) as client:
        response = client.post("/api/v1/sources", json={"url": "http://169.254.169.254/meta"})

    assert response.status_code == 401
