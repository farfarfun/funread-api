"""Both auth paths: the admin console's shared password and reader accounts.

The two must stay apart -- an admin cookie is not a reader identity and a
reader cookie does not unlock the console -- and per-user state must never leak
between accounts.

Reader credentials themselves are funauth's, and funauth tests bcrypt, invite
consumption and the timing-equalised failure paths on its own side. What is
tested here is what this service adds on top: who may create the *first*
account, the pre-funauth password upgrade, and the two cookies' boundaries.
"""

import time

import pytest
from accounts_support import (
    issue_invite,
    legacy_scrypt_hash,
    make_account,
    read_password_hash,
    set_active,
    set_password_hash,
)
from fastapi.testclient import TestClient
from funauth import UserRole

from funread.legado.reader import storage
from funread_api.app import create_app
from funread_api.security import COOKIE_NAME, issue_token, verify_token


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolate the DB. Auth env is already neutralised by the autouse conftest fixture."""
    url = f"sqlite:///{tmp_path / 'auth.db'}"
    monkeypatch.setenv("FUNREAD_DATABASE_URL", url)
    monkeypatch.setenv("FUNREAD_CACHE_ROOT", str(tmp_path / "hubs"))
    monkeypatch.setattr(storage, "_INITIALIZED_DATABASES", set())
    return monkeypatch


def _register(client, username="alice", password="password123", code=""):
    return client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": password, "code": code},
    )


def _bootstrap(client, username="alice", password="password123"):
    """Claim a fresh install: the first account needs no invite code."""
    response = _register(client, username=username, password=password)
    assert response.status_code == 201, response.text
    return response


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


# ------------------------------------------------------------------ 首个账号


def test_the_first_account_needs_no_invite_code(env):
    """There is nobody to issue one yet, so requiring one would be a deadlock."""
    with TestClient(create_app()) as client:
        body = _bootstrap(client).json()

        assert body["authenticated"] is True
        assert body["username"] == "alice"
        assert body["local"] is False
        assert body["user_id"] > 0
        #  Whoever claims the box is its admin -- they can then issue codes.
        assert body["role"] == UserRole.ADMIN
        assert client.get("/api/v1/shelf").status_code == 200


def test_bootstrapping_requires_the_admin_password_when_one_is_set(env):
    """Otherwise a registration would walk straight through a deliberate lockdown."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")

    with TestClient(create_app()) as client:
        refused = _register(client)
        assert refused.status_code == 403
        assert "管理口令" in refused.json()["detail"]

        client.post("/api/v1/auth/admin/login", json={"password": "hunter2"})
        assert _register(client).status_code == 201


def test_the_first_account_inherits_the_accountless_era_data(env):
    """The shelf built before accounts existed belongs to whoever registers first."""
    with TestClient(create_app()) as client:
        added = client.post("/api/v1/shelf", json={"name": "剑来", "author": "烽火"})
        assert added.status_code == 201

        _bootstrap(client)

        shelf = client.get("/api/v1/shelf").json()

    assert [book["name"] for book in shelf] == ["剑来"]


# ------------------------------------------------------------------ 邀请码注册


def test_the_second_account_needs_a_valid_invite_code(env):
    with TestClient(create_app()) as client:
        _bootstrap(client)
        client.post("/api/v1/auth/logout")

        assert _register(client, username="bob").status_code == 400
        assert _register(client, username="bob", code="NOPE").status_code == 400

        code = issue_invite()
        created = _register(client, username="bob", code=code)

    assert created.status_code == 201
    #  Self-service registration never produces an admin, whoever handed the
    #  code out -- a leaked code must not be a leaked console.
    assert created.json()["role"] == UserRole.GUEST


def test_an_invite_code_is_spent_once(env):
    with TestClient(create_app()) as client:
        _bootstrap(client)
        code = issue_invite(max_uses=1)

        assert _register(client, username="bob", code=code).status_code == 201
        second = _register(client, username="carol", code=code)

    assert second.status_code == 400


def test_a_username_collision_gives_the_invite_slot_back(env):
    """Someone fat-fingering a taken username must not burn the code."""
    with TestClient(create_app()) as client:
        _bootstrap(client, username="alice")
        code = issue_invite(max_uses=1)

        collision = _register(client, username="alice", code=code)
        assert collision.status_code == 409
        assert "已存在" in collision.json()["detail"]

        assert _register(client, username="bob", code=code).status_code == 201


def test_registration_can_be_closed_outright(env):
    """``FUNREAD_REGISTER_OPEN=0`` shuts the door even on a live code."""
    with TestClient(create_app()) as client:
        _bootstrap(client)
        code = issue_invite()
        env.setenv("FUNREAD_REGISTER_OPEN", "0")

        refused = _register(client, username="bob", code=code)

    assert refused.status_code == 403
    assert "FUNREAD_REGISTER_OPEN" in refused.json()["detail"]


@pytest.mark.parametrize(
    ("username", "password"),
    [("ab", "password123"), ("has space", "password123"), ("alice", "short")],
)
def test_bad_registration_input_is_a_400(env, username, password):
    with TestClient(create_app()) as client:
        response = _register(client, username=username, password=password)

    assert response.status_code == 400


def test_accounts_probe_reports_a_count_not_a_user_list(env):
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/auth/accounts").json() == {
            "users": 0,
            "register_open": True,
            "min_password_length": 8,
            #  Tells the sign-up form not to ask for an invite code yet
            "bootstrap": True,
        }
        _bootstrap(client)
        body = client.get("/api/v1/auth/accounts").json()

    assert body == {
        "users": 1,
        "register_open": True,
        "min_password_length": 8,
        "bootstrap": False,
    }
    assert "alice" not in str(body)


# ------------------------------------------------------------------ 读者登录


def test_reader_login_then_logout(env):
    with TestClient(create_app()) as client:
        _bootstrap(client)
        client.post("/api/v1/auth/logout")
        assert client.get("/api/v1/shelf").status_code == 401

        login = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "password123"}
        )
        assert login.status_code == 200
        assert login.json()["username"] == "alice"
        assert client.get("/api/v1/shelf").status_code == 200


def test_reader_login_failures_are_indistinguishable(env):
    """Three different causes, one answer -- otherwise this is a username oracle."""
    make_account("alice", "password123")
    make_account("carol", "password123", UserRole.GUEST)

    with TestClient(create_app()) as client:
        wrong_password = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "nope"}
        )
        unknown_user = client.post(
            "/api/v1/auth/login", json={"username": "bob", "password": "password123"}
        )

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_a_disabled_account_cannot_log_in(env):
    make_account("alice", "password123")
    set_active("alice", False)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "password123"}
        )

    assert response.status_code == 401


def test_disabling_an_account_kills_its_live_session(env):
    """The session carries only the id, so the role and the enabled flag are
    re-read every request -- that is the whole reason it carries only the id."""
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        assert client.get("/api/v1/shelf").status_code == 200

        set_active("alice", False)

        assert client.get("/api/v1/shelf").status_code == 401


def test_once_an_account_exists_the_local_fallback_is_gone(env):
    """Otherwise the first account's shelf would be readable by anyone."""
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/shelf").status_code == 401
        assert client.get("/api/v1/auth/me").json()["authenticated"] is False


def test_a_forged_session_cookie_does_not_get_in(env):
    """The session cookie is signed by Starlette; an unsigned one is ignored."""
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.cookies.set("session", "eyJ1c2VyX2lkIjogIjEifQ==")
        assert client.get("/api/v1/shelf").status_code == 401


# ------------------------------------------------------------------ 旧口令透明升级


def test_a_pre_funauth_scrypt_password_still_logs_in_and_gets_upgraded(env):
    """Upgrading the package must not make everyone reset their password."""
    make_account("alice", "placeholder-password")
    set_password_hash("alice", legacy_scrypt_hash("password123"))
    assert read_password_hash("alice").startswith("scrypt$")

    with TestClient(create_app()) as client:
        login = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "password123"}
        )
        assert login.status_code == 200
        assert login.json()["username"] == "alice"
        assert client.get("/api/v1/shelf").status_code == 200

    #  Rewritten with bcrypt on the way through, so this path runs once per account
    assert not read_password_hash("alice").startswith("scrypt$")


def test_a_wrong_password_against_a_scrypt_hash_is_a_plain_401(env):
    """Not a 500 -- funauth's bcrypt verifier never sees the legacy string."""
    make_account("alice", "placeholder-password")
    set_password_hash("alice", legacy_scrypt_hash("password123"))

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "wrong-one"}
        )

    assert response.status_code == 401
    assert read_password_hash("alice").startswith("scrypt$")


def test_a_legacy_account_is_not_distinguishable_by_its_failure_message(env):
    """A differently-worded 401 would confirm "this account predates funauth",
    which is one confirmation of the account existing at all."""
    make_account("alice", "placeholder-password")
    set_password_hash("alice", legacy_scrypt_hash("password123"))
    make_account("carol", "password123")

    with TestClient(create_app()) as client:
        legacy = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "wrong-one"}
        )
        bcrypt = client.post(
            "/api/v1/auth/login", json={"username": "carol", "password": "wrong-one"}
        )
        unknown = client.post(
            "/api/v1/auth/login", json={"username": "nobody", "password": "wrong-one"}
        )

    assert legacy.status_code == bcrypt.status_code == unknown.status_code == 401
    assert legacy.json() == bcrypt.json() == unknown.json()


def test_a_disabled_legacy_account_cannot_log_in(env):
    """`is_active` is checked before the legacy verifier, not after it."""
    make_account("alice", "placeholder-password")
    set_password_hash("alice", legacy_scrypt_hash("password123"))
    set_active("alice", False)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "password123"}
        )

    assert response.status_code == 401
    #  Still scrypt: a disabled account must not get its hash rewritten either.
    assert read_password_hash("alice").startswith("scrypt$")


# ------------------------------------------------------------------ 两端互不越界


def test_an_admin_cookie_is_not_a_reader_identity(env):
    """Unlocking the console must not hand over someone's shelf."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/admin/login", json={"password": "hunter2"})
        assert client.get("/api/v1/sources").status_code == 200
        assert client.get("/api/v1/shelf").status_code == 401


def test_a_reader_cookie_does_not_unlock_the_console(env):
    """Reading is not administering -- POST /sources is an SSRF primitive."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "alice", "password": "password123"}
            ).status_code
            == 200
        )
        assert client.get("/api/v1/shelf").status_code == 200
        assert client.get("/api/v1/sources").status_code == 401
        assert (
            client.post("/api/v1/sources", json={"url": "http://169.254.169.254/meta"}).status_code
            == 401
        )


# ------------------------------------------------------------------ 跨用户隔离


def test_shelves_do_not_leak_between_accounts(env):
    make_account("alice", "password123")
    make_account("bob", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        added = client.post("/api/v1/shelf", json={"name": "剑来", "author": "烽火"})
        assert added.status_code == 201
        book_key = added.json()["book_key"]
        assert len(client.get("/api/v1/shelf").json()) == 1

        client.post("/api/v1/auth/logout")
        client.post("/api/v1/auth/login", json={"username": "bob", "password": "password123"})
        assert client.get("/api/v1/shelf").json() == []
        #  Knowing the key is not access
        assert (
            client.put(f"/api/v1/shelf/{book_key}/progress", json={"chapter_index": 1}).status_code
            == 404
        )
        assert client.delete(f"/api/v1/shelf/{book_key}").status_code == 404
        assert client.get(f"/api/v1/shelf/{book_key}/cached").status_code == 404


def test_progress_is_per_account(env):
    make_account("alice", "password123")
    make_account("bob", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        book_key = client.post("/api/v1/shelf", json={"name": "剑来"}).json()["book_key"]
        client.put(f"/api/v1/shelf/{book_key}/progress", json={"chapter_index": 11})
        assert client.get("/api/v1/shelf").json()[0]["progress"]["chapter_index"] == 11

        client.post("/api/v1/auth/logout")
        client.post("/api/v1/auth/login", json={"username": "bob", "password": "password123"})
        client.post("/api/v1/shelf", json={"name": "剑来"})
        assert client.get("/api/v1/shelf").json()[0]["progress"] is None


def test_shelf_groups_are_per_account(env):
    """分组名本身也是个人数据 —— 别人架上有「政治」这一组不该被看见。"""
    make_account("alice", "password123")
    make_account("bob", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        book_key = client.post("/api/v1/shelf", json={"name": "剑来"}).json()["book_key"]
        client.post("/api/v1/shelf/groups/assign", json={"book_keys": [book_key], "group": "玄幻"})
        assert client.get("/api/v1/shelf/groups").json() == [{"name": "玄幻", "count": 1}]

        client.post("/api/v1/auth/logout")
        client.post("/api/v1/auth/login", json={"username": "bob", "password": "password123"})
        assert client.get("/api/v1/shelf/groups").json() == []
        #  知道 book_key 也动不了别人的书
        assert client.post(
            "/api/v1/shelf/groups/assign", json={"book_keys": [book_key], "group": "都市"}
        ).json() == {"affected": 0}
        #  知道组名也改不了别人的组
        assert (
            client.post(
                "/api/v1/shelf/groups/rename", json={"old": "玄幻", "new": "被改了"}
            ).status_code
            == 404
        )

        client.post("/api/v1/auth/logout")
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        assert client.get("/api/v1/shelf").json()[0]["group"] == "玄幻"


def test_an_update_check_task_belongs_to_the_account_that_started_it(env):
    from funread_api.v1.deps import get_update_check_tracker

    alice = make_account("alice", "password123")
    make_account("bob", "password123")
    #  alice 名下的一个任务，bob 不该查得到进度
    task = get_update_check_tracker().start("", alice, 1)

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "bob", "password": "password123"})
        assert client.get(f"/api/v1/shelf/check-updates/{task.task_id}").status_code == 404
        #  并且 alice 的那一轮不挡 bob 自己开一轮
        client.post("/api/v1/shelf", json={"name": "剑来"})
        assert client.post("/api/v1/shelf/check-updates", json={"interval": 0}).status_code == 202


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
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        assert client.post("/api/v1/reader/scan").status_code == 401
        assert client.get("/api/v1/shelf").status_code == 401
        assert client.post("/api/v1/shelf", json={"name": "剑来"}).status_code == 401


def test_reader_public_does_not_open_registration(env):
    """Bootstrapping is still gated on the admin password, public or not."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")
    env.setenv("FUNREAD_READER_PUBLIC", "1")

    with TestClient(create_app()) as client:
        assert _register(client).status_code == 403


def test_reader_public_does_not_open_the_ssrf_endpoint(env):
    """POST /sources fetches an arbitrary URL server-side; it stays locked."""
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")
    env.setenv("FUNREAD_READER_PUBLIC", "1")

    with TestClient(create_app()) as client:
        response = client.post("/api/v1/sources", json={"url": "http://169.254.169.254/meta"})

    assert response.status_code == 401


def test_the_database_is_not_selectable_from_the_query_string(env, tmp_path):
    """没有一个端点可以让调用方指定连哪个库。

    `get_session` 曾经写成 `get_session(database_url=None)` 并直接挂成 FastAPI
    依赖。FastAPI 把依赖签名里带默认值的标量当查询参数公开，于是 39 个端点上都多
    出一个 `?database_url=`：谁都能让本服务向他给的主机发起外联，再把注册/登录引
    到他自己的库上，换一张本服务认的 cookie 回来。所以这里既查契约（不许出现在
    openapi 里），也查行为（真传了也不生效）。
    """
    env.setenv("FUNREAD_API_PASSWORD", "hunter2")
    make_account("alice", "password123")
    elsewhere = tmp_path / "elsewhere.db"

    with TestClient(create_app()) as client:
        exposed = [
            f"{method} {route}"
            for route, item in client.app.openapi()["paths"].items()
            for method, operation in item.items()
            for parameter in operation.get("parameters", [])
            if parameter["in"] == "query" and parameter["name"] == "database_url"
        ]
        assert exposed == []

        #  传了也必须被当成无关的查询串忽略，而不是换一个库。
        assert (
            client.post(
                "/api/v1/auth/login",
                params={"database_url": f"sqlite:///{elsewhere}"},
                json={"username": "alice", "password": "password123"},
            ).status_code
            == 200
        )

    assert not elsewhere.exists()
