"""Both auth paths: the admin console's shared password and reader accounts.

The two must stay apart -- an admin cookie is not a reader identity and a
reader cookie does not unlock the console -- and per-user state must never leak
between accounts.
"""

import time

import pytest
from fastapi.testclient import TestClient

from funread.legado.reader import create_user, storage
from funread_api.app import create_app
from funread_api.security import (
    COOKIE_NAME,
    USER_COOKIE_NAME,
    issue_token,
    issue_user_token,
    resolve_user_token,
    verify_token,
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolate the DB. Auth env is already neutralised by the autouse conftest fixture."""
    url = f"sqlite:///{tmp_path / 'auth.db'}"
    monkeypatch.setenv("FUNREAD_DATABASE_URL", url)
    monkeypatch.setenv("FUNREAD_CACHE_ROOT", str(tmp_path / "hubs"))
    monkeypatch.setattr(storage, "_INITIALIZED_DATABASES", set())
    monkeypatch.delenv("FUNREAD_REGISTER_CODE", raising=False)
    return monkeypatch


def _register(client, username="alice", password="password123", code="letmein"):
    return client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": password, "code": code},
    )


# ------------------------------------------------------------------ admin token


def test_token_round_trips():
    assert verify_token(issue_token("secret"), "secret") is True


def test_token_is_rejected_under_another_password():
    """Changing the password is the only session revocation there is."""
    assert verify_token(issue_token("secret"), "other") is False


def test_expired_token_is_rejected():
    token = issue_token("secret", now=time.time() - 365 * 24 * 3600)
    assert verify_token(token, "secret") is False


@pytest.mark.parametrize("token", ["", "garbage", "notanint.abc", "123"])
def test_malformed_admin_tokens_are_rejected(token):
    assert verify_token(token, "secret") is False


# ------------------------------------------------------------------ reader token


def test_user_token_round_trips(env):
    user = create_user("alice", "password123")
    token = issue_user_token(user.user_id, user.password_hash)
    resolved = resolve_user_token(token)
    assert resolved is not None
    assert (resolved.user_id, resolved.username) == (user.user_id, "alice")


def test_user_token_dies_with_a_password_change(env):
    """The hash is the signing key, so a new password revokes old sessions."""
    user = create_user("alice", "password123")
    token = issue_user_token(user.user_id, user.password_hash)
    session_factory = storage.get_session_factory(None)
    with session_factory() as session:
        session.get(storage.ReaderUser, user.user_id).password_hash = storage.hash_password("new")
        session.commit()
    assert resolve_user_token(token) is None


def test_user_token_for_a_deleted_user_is_rejected(env):
    assert resolve_user_token(issue_user_token(999, "scrypt$1$1$1$aa$bb")) is None


def test_user_token_for_a_disabled_user_is_rejected(env):
    user = create_user("alice", "password123")
    token = issue_user_token(user.user_id, user.password_hash)
    session_factory = storage.get_session_factory(None)
    with session_factory() as session:
        session.get(storage.ReaderUser, user.user_id).disabled = True
        session.commit()
    assert resolve_user_token(token) is None


def test_expired_user_token_is_rejected(env):
    user = create_user("alice", "password123")
    token = issue_user_token(user.user_id, user.password_hash, now=time.time() - 365 * 24 * 3600)
    assert resolve_user_token(token) is None


@pytest.mark.parametrize(
    "token", ["", "garbage", "r1.1.2", "r2.1.99999999999.aa", "r1.x.1.aa", "1.2.3.4"]
)
def test_malformed_user_tokens_are_rejected(env, token):
    assert resolve_user_token(token) is None


# ------------------------------------------------------------------ 未配置口令、无账号


def test_a_fresh_install_stays_open(env):
    """Upgrading into accounts must not lock out the existing local setup."""
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/sources").status_code == 200
        assert client.get("/api/v1/shelf").status_code == 200

        state = client.get("/api/v1/auth/me").json()

    assert state["authenticated"] is True
    assert state["local"] is True
    assert state["user_id"] == 0
    assert state["auth_required"] is False
    assert state["register_open"] is False


def test_admin_login_without_a_configured_password_is_a_no_op(env):
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/auth/admin/login", json={"password": "whatever"})

    assert response.status_code == 200
    assert response.json()["auth_required"] is False
    assert COOKIE_NAME not in response.cookies


# ------------------------------------------------------------------ 配置了管理口令


def test_the_admin_password_still_locks_the_shelf_while_no_account_exists(env):
    """A setup that locked everything down with one password must not lose that."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/sources").status_code == 401
        assert client.get("/api/v1/shelf").status_code == 401
        assert client.get("/api/v1/reader/search", params={"keyword": "剑"}).status_code == 401


def test_healthz_and_me_stay_reachable(env):
    """Both are needed before a session exists -- one for probes, one to get in."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/v1/auth/me").json()["authenticated"] is False


def test_admin_login_then_use_then_logout(env):
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        wrong = client.post("/api/v1/auth/admin/login", json={"password": "wrong"})
        assert wrong.status_code == 401

        login = client.post("/api/v1/auth/admin/login", json={"password": "hunter2"})
        assert login.status_code == 200
        assert login.json()["admin"] is True

        #  TestClient keeps the cookie jar, so this is the browser's view
        assert client.get("/api/v1/sources").status_code == 200

        assert client.post("/api/v1/auth/logout").status_code == 204
        assert client.get("/api/v1/sources").status_code == 401


def test_admin_cookie_is_httponly(env):
    """It is only ever read by the server; JS access would just widen XSS."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        login = client.post("/api/v1/auth/admin/login", json={"password": "hunter2"})

    assert "httponly" in login.headers["set-cookie"].lower()


def test_a_forged_admin_cookie_does_not_get_in(env):
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        client.cookies.set(COOKIE_NAME, issue_token("hunter3"))
        assert client.get("/api/v1/sources").status_code == 401


# ------------------------------------------------------------------ 注册


def test_registration_is_closed_unless_a_code_is_configured(env):
    with TestClient(create_app()) as client:
        response = _register(client)

    assert response.status_code == 403
    assert "FUNREAD_REGISTER_CODE" in response.json()["detail"]


def test_registration_needs_the_right_code(env):
    env.setenv("FUNREAD_REGISTER_CODE", "letmein")

    with TestClient(create_app()) as client:
        assert _register(client, code="nope").status_code == 403
        assert _register(client, code="").status_code == 403
        assert _register(client, code="letmein").status_code == 201


def test_registration_logs_you_straight_in(env):
    env.setenv("FUNREAD_REGISTER_CODE", "letmein")

    with TestClient(create_app()) as client:
        response = _register(client)
        assert response.status_code == 201
        body = response.json()
        assert body["authenticated"] is True
        assert body["username"] == "alice"
        assert body["local"] is False
        assert body["user_id"] > 0
        assert "httponly" in response.headers["set-cookie"].lower()

        assert client.get("/api/v1/shelf").status_code == 200


def test_duplicate_username_is_a_conflict(env):
    env.setenv("FUNREAD_REGISTER_CODE", "letmein")

    with TestClient(create_app()) as client:
        assert _register(client).status_code == 201
        again = _register(client)

    assert again.status_code == 409
    assert "已被占用" in again.json()["detail"]


@pytest.mark.parametrize(
    ("username", "password"),
    [("ab", "password123"), ("has space", "password123"), ("alice", "short")],
)
def test_bad_registration_input_is_a_400(env, username, password):
    env.setenv("FUNREAD_REGISTER_CODE", "letmein")

    with TestClient(create_app()) as client:
        response = _register(client, username=username, password=password)

    assert response.status_code == 400


def test_accounts_probe_reports_a_count_not_a_user_list(env):
    env.setenv("FUNREAD_REGISTER_CODE", "letmein")

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/auth/accounts").json() == {
            "users": 0,
            "register_open": True,
            "min_password_length": 8,
        }
        _register(client)
        body = client.get("/api/v1/auth/accounts").json()

    assert body["users"] == 1
    assert "alice" not in str(body)


# ------------------------------------------------------------------ 读者登录


def test_reader_login_then_logout(env):
    env.setenv("FUNREAD_REGISTER_CODE", "letmein")

    with TestClient(create_app()) as client:
        _register(client)
        client.post("/api/v1/auth/logout")
        assert client.get("/api/v1/shelf").status_code == 401

        login = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "password123"}
        )
        assert login.status_code == 200
        assert login.json()["username"] == "alice"
        assert client.get("/api/v1/shelf").status_code == 200


def test_reader_login_failures_are_indistinguishable(env):
    env.setenv("FUNREAD_REGISTER_CODE", "letmein")

    with TestClient(create_app()) as client:
        _register(client)
        wrong_password = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "nope"}
        )
        unknown_user = client.post(
            "/api/v1/auth/login", json={"username": "bob", "password": "password123"}
        )

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_once_an_account_exists_the_local_fallback_is_gone(env):
    """Otherwise the first account's shelf would be readable by anyone."""
    create_user("alice", "password123")

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/shelf").status_code == 401
        assert client.get("/api/v1/auth/me").json()["authenticated"] is False


def test_a_forged_user_cookie_does_not_get_in(env):
    user = create_user("alice", "password123")

    with TestClient(create_app()) as client:
        client.cookies.set(USER_COOKIE_NAME, f"r1.{user.user_id}.99999999999.deadbeef")
        assert client.get("/api/v1/shelf").status_code == 401


# ------------------------------------------------------------------ 两端互不越界


def test_an_admin_cookie_is_not_a_reader_identity(env):
    """Unlocking the console must not hand over someone's shelf."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")
    create_user("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/admin/login", json={"password": "hunter2"})
        assert client.get("/api/v1/sources").status_code == 200
        assert client.get("/api/v1/shelf").status_code == 401


def test_a_reader_cookie_does_not_unlock_the_console(env):
    """Reading is not administering -- POST /sources is an SSRF primitive."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")
    env.setenv("FUNREAD_REGISTER_CODE", "letmein")

    with TestClient(create_app()) as client:
        assert _register(client).status_code == 201
        assert client.get("/api/v1/shelf").status_code == 200
        assert client.get("/api/v1/sources").status_code == 401
        assert (
            client.post("/api/v1/sources", json={"url": "http://169.254.169.254/meta"}).status_code
            == 401
        )


# ------------------------------------------------------------------ 跨用户隔离


def test_shelves_do_not_leak_between_accounts(env):
    env.setenv("FUNREAD_REGISTER_CODE", "letmein")

    with TestClient(create_app()) as alice:
        _register(alice, username="alice")
        added = alice.post("/api/v1/shelf", json={"name": "剑来", "author": "烽火"})
        assert added.status_code == 201
        book_key = added.json()["book_key"]
        assert len(alice.get("/api/v1/shelf").json()) == 1

    with TestClient(create_app()) as bob:
        _register(bob, username="bob")
        assert bob.get("/api/v1/shelf").json() == []
        #  Knowing the key is not access
        assert (
            bob.put(f"/api/v1/shelf/{book_key}/progress", json={"chapter_index": 1}).status_code
            == 404
        )
        assert bob.delete(f"/api/v1/shelf/{book_key}").status_code == 404
        assert bob.get(f"/api/v1/shelf/{book_key}/cached").status_code == 404


def test_progress_is_per_account(env):
    env.setenv("FUNREAD_REGISTER_CODE", "letmein")

    with TestClient(create_app()) as client:
        _register(client, username="alice")
        added = client.post("/api/v1/shelf", json={"name": "剑来"})
        book_key = added.json()["book_key"]
        client.put(f"/api/v1/shelf/{book_key}/progress", json={"chapter_index": 11})
        assert client.get("/api/v1/shelf").json()[0]["progress"]["chapter_index"] == 11

        client.post("/api/v1/auth/logout")
        _register(client, username="bob")
        client.post("/api/v1/shelf", json={"name": "剑来"})
        assert client.get("/api/v1/shelf").json()[0]["progress"] is None


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
    create_user("alice", "password123")

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
