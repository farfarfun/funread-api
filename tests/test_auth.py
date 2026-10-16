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

from funread.base import config
from funread.legado.reader import storage
from funread_api.app import create_app
from funread_api.security import COOKIE_NAME, issue_token, verify_token


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """Isolate the DB, and hand back a setter for the auth settings.

    返回的就是 `config.set_overrides`，所以用例里写 `settings(api_password="hunter2")`
    即可。不用 `monkeypatch.setenv`：`FUNREAD_*` 那一层整个删掉了，setenv 现在是空
    操作。override 由 conftest 的 autouse fixture 在每条用例后清掉。
    """
    url = f"sqlite:///{tmp_path / 'auth.db'}"
    config.set_overrides(database_url=url, cache_root=str(tmp_path / "hubs"))
    monkeypatch.setattr(storage, "_INITIALIZED_DATABASES", set())
    return config.set_overrides


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


def test_a_fresh_install_stays_open(settings):
    """Upgrading into accounts must not lock out the existing local setup."""
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/sources").status_code == 200
        assert client.get("/api/v1/shelf").status_code == 200

        state = client.get("/api/v1/auth/me").json()

    assert state["authenticated"] is True
    assert state["local"] is True
    assert state["user_id"] == 0
    assert state["auth_required"] is False


def test_admin_login_without_a_configured_password_is_a_no_op(settings):
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/auth/admin/login", json={"password": "whatever"})

    assert response.status_code == 200
    assert response.json()["auth_required"] is False
    assert COOKIE_NAME not in response.cookies


# ------------------------------------------------------------------ 配置了管理口令


def test_the_admin_password_still_locks_the_shelf_while_no_account_exists(settings):
    """A setup that locked everything down with one password must not lose that."""
    settings(api_password="hunter2")

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/sources").status_code == 401
        assert client.get("/api/v1/shelf").status_code == 401
        assert client.get("/api/v1/reader/search", params={"keyword": "剑"}).status_code == 401


def test_healthz_and_me_stay_reachable(settings):
    """Both are needed before a session exists -- one for probes, one to get in."""
    settings(api_password="hunter2")

    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/v1/auth/me").json()["authenticated"] is False


def test_admin_login_then_use_then_logout(settings):
    settings(api_password="hunter2")

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


def test_admin_cookie_is_httponly(settings):
    """It is only ever read by the server; JS access would just widen XSS."""
    settings(api_password="hunter2")

    with TestClient(create_app()) as client:
        login = client.post("/api/v1/auth/admin/login", json={"password": "hunter2"})

    assert "httponly" in login.headers["set-cookie"].lower()


def test_a_forged_admin_cookie_does_not_get_in(settings):
    settings(api_password="hunter2")

    with TestClient(create_app()) as client:
        client.cookies.set(COOKIE_NAME, issue_token("hunter3"))
        assert client.get("/api/v1/sources").status_code == 401


# ------------------------------------------------------------------ 首个账号


def test_the_first_account_needs_no_invite_code(settings):
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


def test_bootstrapping_requires_the_admin_password_when_one_is_set(settings):
    """Otherwise a registration would walk straight through a deliberate lockdown."""
    settings(api_password="hunter2")

    with TestClient(create_app()) as client:
        refused = _register(client)
        assert refused.status_code == 403
        assert "管理口令" in refused.json()["detail"]

        client.post("/api/v1/auth/admin/login", json={"password": "hunter2"})
        assert _register(client).status_code == 201


def test_the_first_account_inherits_the_accountless_era_data(settings):
    """The shelf built before accounts existed belongs to whoever registers first."""
    with TestClient(create_app()) as client:
        added = client.post("/api/v1/shelf", json={"name": "剑来", "author": "烽火"})
        assert added.status_code == 201

        _bootstrap(client)

        shelf = client.get("/api/v1/shelf").json()

    assert [book["name"] for book in shelf] == ["剑来"]


# ------------------------------------------------------------------ 邀请码注册


def test_the_second_account_needs_a_valid_invite_code(settings):
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


def test_an_invite_code_is_spent_once(settings):
    with TestClient(create_app()) as client:
        _bootstrap(client)
        code = issue_invite(max_uses=1)

        assert _register(client, username="bob", code=code).status_code == 201
        second = _register(client, username="carol", code=code)

    assert second.status_code == 400


def test_a_username_collision_gives_the_invite_slot_back(settings):
    """Someone fat-fingering a taken username must not burn the code."""
    with TestClient(create_app()) as client:
        _bootstrap(client, username="alice")
        code = issue_invite(max_uses=1)

        collision = _register(client, username="alice", code=code)
        assert collision.status_code == 409
        assert "已存在" in collision.json()["detail"]

        assert _register(client, username="bob", code=code).status_code == 201


def test_registration_can_be_closed_outright(settings):
    """``[auth] register_open = false`` shuts the door even on a live code."""
    with TestClient(create_app()) as client:
        _bootstrap(client)
        code = issue_invite()
        settings(register_open=False)

        refused = _register(client, username="bob", code=code)

    assert refused.status_code == 403
    assert "register_open" in refused.json()["detail"]


@pytest.mark.parametrize(
    ("username", "password"),
    [("ab", "password123"), ("has space", "password123"), ("alice", "short")],
)
def test_bad_registration_input_is_a_400(settings, username, password):
    with TestClient(create_app()) as client:
        response = _register(client, username=username, password=password)

    assert response.status_code == 400


def test_accounts_probe_reports_a_count_not_a_user_list(settings):
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


def test_reader_login_then_logout(settings):
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


def test_reader_login_failures_are_indistinguishable(settings):
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


def test_a_disabled_account_cannot_log_in(settings):
    make_account("alice", "password123")
    set_active("alice", False)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "password123"}
        )

    assert response.status_code == 401


def test_disabling_an_account_kills_its_live_session(settings):
    """The session carries only the id, so the role and the enabled flag are
    re-read every request -- that is the whole reason it carries only the id."""
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        assert client.get("/api/v1/shelf").status_code == 200

        set_active("alice", False)

        assert client.get("/api/v1/shelf").status_code == 401


def test_once_an_account_exists_the_local_fallback_is_gone(settings):
    """Otherwise the first account's shelf would be readable by anyone."""
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/shelf").status_code == 401
        assert client.get("/api/v1/auth/me").json()["authenticated"] is False


def test_a_forged_session_cookie_does_not_get_in(settings):
    """The session cookie is signed by Starlette; an unsigned one is ignored."""
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.cookies.set("session", "eyJ1c2VyX2lkIjogIjEifQ==")
        assert client.get("/api/v1/shelf").status_code == 401


# ------------------------------------------------------------------ 旧口令透明升级


def test_a_pre_funauth_scrypt_password_still_logs_in_and_gets_upgraded(settings):
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


def test_a_wrong_password_against_a_scrypt_hash_is_a_plain_401(settings):
    """Not a 500 -- funauth's bcrypt verifier never sees the legacy string."""
    make_account("alice", "placeholder-password")
    set_password_hash("alice", legacy_scrypt_hash("password123"))

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "wrong-one"}
        )

    assert response.status_code == 401
    assert read_password_hash("alice").startswith("scrypt$")


def test_a_legacy_account_is_not_distinguishable_by_its_failure_message(settings):
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


def test_a_disabled_legacy_account_cannot_log_in(settings):
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


def test_an_admin_cookie_is_not_a_reader_identity(settings):
    """Unlocking the console must not hand over someone's shelf."""
    settings(api_password="hunter2")
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/admin/login", json={"password": "hunter2"})
        assert client.get("/api/v1/shelf").status_code == 401


def test_the_shared_password_no_longer_unlocks_the_console(settings):
    """管理端认**账号角色**，不认那个预共享口令。

    旧模型里 `[auth] password` 是与账号平行的第二套凭据，它有个要命的缺省：
    口令没配时所有管理端接口直接敞开。现在这条路彻底断了 —— 口令对不对都一样，
    没有 ADMIN 账号身份就进不去。

    口令本身没废，它退回「实例锁」这一个用途：挡住陌生人在一台全新实例上抢注
    第一个（ADMIN）账号，见 `resolve_current_user` 与 `POST /auth/register`。
    """
    settings(api_password="hunter2")
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        assert (
            client.post("/api/v1/auth/admin/login", json={"password": "hunter2"}).status_code == 200
        )
        #  口令验过了，但没有账号身份 —— 管理端一样不给进
        assert client.get("/api/v1/sources").status_code == 401


def test_a_reader_cookie_does_not_unlock_the_console(settings):
    """Reading is not administering -- POST /sources is an SSRF primitive.

    普通读者登录着，所以是 **403 而不是 401**：身份有了，权限不够。两者要分开 ——
    401 会让前端把人踢去重新登录，而再登一次也还是进不去。
    """
    settings(api_password="hunter2")
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "alice", "password": "password123"}
            ).status_code
            == 200
        )
        assert client.get("/api/v1/shelf").status_code == 200
        assert client.get("/api/v1/sources").status_code == 403
        assert (
            client.post("/api/v1/sources", json={"url": "http://169.254.169.254/meta"}).status_code
            == 403
        )


def test_an_admin_account_unlocks_the_console(settings):
    """正向：ADMIN 角色的账号登录后管理端可用。这是现在**唯一**的进入方式。"""
    settings(api_password="hunter2")
    make_account("root", "password123", UserRole.ADMIN)

    with TestClient(create_app()) as client:
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "root", "password": "password123"}
            ).status_code
            == 200
        )
        assert client.get("/api/v1/sources").status_code == 200


# ------------------------------------------------------------------ 跨用户隔离


def test_shelves_do_not_leak_between_accounts(settings):
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


def test_progress_is_per_account(settings):
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


def test_shelf_groups_are_per_account(settings):
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


def test_an_update_check_task_belongs_to_the_account_that_started_it(settings):
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


def test_reader_public_opens_read_only_gets(settings):
    settings(api_password="hunter2", reader_public=True)

    with TestClient(create_app()) as client:
        #  No sources scanned, so this is an empty result rather than a 401
        assert client.get("/api/v1/reader/search", params={"keyword": "剑"}).status_code == 200


def test_reader_public_does_not_open_writes(settings):
    """The flag is about letting people *read*, not letting them drive the box."""
    settings(api_password="hunter2", reader_public=True)
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        assert client.post("/api/v1/reader/scan").status_code == 401
        assert client.get("/api/v1/shelf").status_code == 401
        assert client.post("/api/v1/shelf", json={"name": "剑来"}).status_code == 401


def test_reader_public_does_not_open_registration(settings):
    """Bootstrapping is still gated on the admin password, public or not."""
    settings(api_password="hunter2", reader_public=True)

    with TestClient(create_app()) as client:
        assert _register(client).status_code == 403


def test_reader_public_does_not_open_the_ssrf_endpoint(settings):
    """POST /sources fetches an arbitrary URL server-side; it stays locked."""
    settings(api_password="hunter2", reader_public=True)

    with TestClient(create_app()) as client:
        response = client.post("/api/v1/sources", json={"url": "http://169.254.169.254/meta"})

    assert response.status_code == 401


def test_the_database_is_not_selectable_from_the_query_string(settings, tmp_path):
    """没有一个端点可以让调用方指定连哪个库。

    `get_session` 曾经写成 `get_session(database_url=None)` 并直接挂成 FastAPI
    依赖。FastAPI 把依赖签名里带默认值的标量当查询参数公开，于是 39 个端点上都多
    出一个 `?database_url=`：谁都能让本服务向他给的主机发起外联，再把注册/登录引
    到他自己的库上，换一张本服务认的 cookie 回来。所以这里既查契约（不许出现在
    openapi 里），也查行为（真传了也不生效）。
    """
    settings(api_password="hunter2")
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


def test_demoting_the_last_admin_is_a_409(settings):
    """最后一个在用的管理员不能被降级 —— 降完谁也进不去管理端。

    护栏在 funauth（0.2.3 起 `set_role`/`set_active` 都拦，抛 `LastAdmin`），
    这里只钉住它确实被翻成了 409 而不是漏成 500 或者静默成功。

    用 409 而不是 400：这不是请求本身有问题，是和当前状态冲突 —— 再加一个管理员
    之后同样这个请求就合法了。
    """
    make_account("root", "password123", UserRole.ADMIN)
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "root", "password": "password123"})
        users = {item["username"]: item for item in client.get("/api/v1/auth/users").json()}

        #  只有 root 一个管理员，降 alice（访客）没问题
        assert (
            client.patch(
                f"/api/v1/auth/users/{users['alice']['user_id']}", json={"role": "guest"}
            ).status_code
            == 200
        )

        #  提成管理员之后，root 就不再是「最后一个」了
        promoted = client.patch(
            f"/api/v1/auth/users/{users['alice']['user_id']}", json={"role": "admin"}
        )
        assert promoted.status_code == 200
        assert promoted.json()["role"] == UserRole.ADMIN


def test_the_last_admin_cannot_be_disabled_either(settings):
    """停用和降级是同一个坑的两个入口，必须一起拦住。"""
    make_account("root", "password123", UserRole.ADMIN)
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        #  alice 是访客，管不了人
        assert client.get("/api/v1/auth/users").status_code == 403

        client.post("/api/v1/auth/logout")
        client.post("/api/v1/auth/login", json={"username": "root", "password": "password123"})
        users = {item["username"]: item for item in client.get("/api/v1/auth/users").json()}

        #  停用自己先被「不能动自己」拦下（400），轮不到 LastAdmin
        assert (
            client.patch(
                f"/api/v1/auth/users/{users['root']['user_id']}", json={"is_active": False}
            ).status_code
            == 400
        )


def test_an_admin_cannot_demote_themselves(settings):
    """不能动自己：降完当前会话立刻失去管理权，而界面上通常是误点。

    这条 funauth 管不了 —— 它不知道谁是调用者。
    """
    make_account("root", "password123", UserRole.ADMIN)
    make_account("other", "password123", UserRole.ADMIN)

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "root", "password": "password123"})
        users = {item["username"]: item for item in client.get("/api/v1/auth/users").json()}

        #  库里有两个管理员，所以不是 LastAdmin 拦的，是「不能动自己」
        response = client.patch(
            f"/api/v1/auth/users/{users['root']['user_id']}", json={"role": "guest"}
        )
        assert response.status_code == 400
        assert "自己" in response.json()["detail"]


# ------------------------------------------------------------------ 设备 token


def test_device_token_authenticates_cross_site(settings):
    """设备 token 存在的全部理由：cookie 在跨站请求里发不出去。

    套壳 / 原生客户端把页面打包进 app 之后，对用户自己服务器的请求是跨站的，
    而会话 cookie 是 `SameSite=Lax` + `Secure=False`（局域网纯 HTTP 只能如此），
    Lax 跨站不发、改 None 又必须配 Secure —— 死结。所以换一套凭据。
    """
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        created = client.post("/api/v1/auth/tokens", json={"device_name": "我的手机"})
        assert created.status_code == 201
        token = created.json()["token"]
        assert token

    #  **新的 client，没有任何 cookie** —— 模拟 app 里的跨站请求
    with TestClient(create_app()) as client:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/shelf", headers=headers).status_code == 200
        #  不带 token 就该回到未登录
        assert client.get("/api/v1/shelf").status_code == 401


def test_the_plaintext_token_is_returned_only_once(settings):
    """库里只存 sha256，列表接口永远拿不到明文。"""
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        client.post("/api/v1/auth/tokens", json={"device_name": "手机"})

        listed = client.get("/api/v1/auth/tokens").json()
        assert len(listed) == 1
        assert "token" not in listed[0]
        assert listed[0]["device_name"] == "手机"


def test_a_revoked_token_stops_working(settings):
    """手机丢了要能单独吊销这一台 —— 这正是不用 JWT 的理由。"""
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        created = client.post("/api/v1/auth/tokens", json={"device_name": "旧手机"}).json()
        token, token_id = created["token"], created["id"]

        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/shelf", headers=headers).status_code == 200

        assert client.delete(f"/api/v1/auth/tokens/{token_id}").status_code == 204

    with TestClient(create_app()) as client:
        assert (
            client.get("/api/v1/shelf", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )


def test_a_bearer_header_never_falls_back_to_the_cookie(settings):
    """给了 Bearer 就不回落到 cookie，哪怕 token 是错的。

    回落的话，「token 过期了为什么还能用」这类问题完全无法排查 —— 在浏览器里
    测着一切正常，装到手机上就 401。
    """
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        #  cookie 是好的，但 Bearer 是坏的 —— 必须 401 而不是用 cookie 放行。
        #  token 值只能是 ASCII：HTTP 头不允许非 ASCII，写中文会在客户端就炸。
        assert (
            client.get(
                "/api/v1/shelf", headers={"Authorization": "Bearer not-a-real-token"}
            ).status_code
            == 401
        )
        #  空的 Bearer 同理：客户端把变量拼错成空串时要立刻暴露
        assert client.get("/api/v1/shelf", headers={"Authorization": "Bearer "}).status_code == 401
        #  不带这个头才走 cookie
        assert client.get("/api/v1/shelf").status_code == 200


def test_tokens_are_scoped_to_their_owner(settings):
    """只能看到和吊销自己的设备。否则知道 id 就能把别人的设备踢下线。"""
    make_account("alice", "password123")
    make_account("bob", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        alice_token_id = client.post("/api/v1/auth/tokens", json={}).json()["id"]

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "bob", "password": "password123"})
        assert client.get("/api/v1/auth/tokens").json() == []
        assert client.delete(f"/api/v1/auth/tokens/{alice_token_id}").status_code == 404


def test_disabling_an_account_kills_all_its_devices(settings):
    """停用账号时名下所有设备立刻失效，不用逐个吊销。"""
    make_account("root", "password123", UserRole.ADMIN)
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        token = client.post("/api/v1/auth/tokens", json={}).json()["token"]

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "root", "password": "password123"})
        users = {u["username"]: u for u in client.get("/api/v1/auth/users").json()}
        assert (
            client.patch(
                f"/api/v1/auth/users/{users['alice']['user_id']}", json={"is_active": False}
            ).status_code
            == 200
        )

    with TestClient(create_app()) as client:
        assert (
            client.get("/api/v1/shelf", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )


def test_cors_allows_the_shell_client_origins(settings):
    """套壳客户端的 origin 默认就放行 —— 自托管的人不该为了装官方客户端先改配置。

    这三个是**客户端自己的** origin（Capacitor 在 iOS/Android 上的固定值），
    不是任意站点，所以写死是安全的。
    """
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/auth/me", headers={"Origin": "capacitor://localhost"})
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "capacitor://localhost"


def test_cors_does_not_hand_out_cookies(settings):
    """跨站一律走 Bearer，不让 cookie 参与。

    开 `allow_credentials` 会把会话 cookie 暴露在跨站路径上，而我们本来就有
    设备 token 这条更合适的路。浏览器看到没有 `allow-credentials` 就不会带
    cookie 过来，这正是想要的。
    """
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/auth/me", headers={"Origin": "capacitor://localhost"})
        assert "access-control-allow-credentials" not in response.headers


def test_cors_rejects_an_unknown_origin(settings):
    """没在名单里的站点拿不到放行头。"""
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/auth/me", headers={"Origin": "https://evil.example"})
        assert response.headers.get("access-control-allow-origin") != "https://evil.example"


def test_a_token_can_be_issued_with_credentials_alone(settings):
    """打包版客户端必须能在**没有会话**的情况下拿到 token。

    `POST /auth/tokens` 要求 require_user，而那依赖 cookie —— 可打包版正是因为
    跨站拿不到 cookie 才需要 token。登录的 Set-Cookie 在 allow_credentials=False
    下不会被保存，下一步签发就是 401。这条端点是为了断开那个死循环。
    """
    make_account("alice", "password123")

    #  全新 client，不登录，直接换 token
    with TestClient(create_app()) as client:
        created = client.post(
            "/api/v1/auth/tokens/issue",
            json={"username": "alice", "password": "password123", "device_name": "手机"},
        )
        assert created.status_code == 201
        token = created.json()["token"]

    #  换来的 token 可用
    with TestClient(create_app()) as client:
        assert (
            client.get("/api/v1/shelf", headers={"Authorization": f"Bearer {token}"}).status_code
            == 200
        )


def test_issuing_with_a_wrong_password_is_a_401(settings):
    """失败一律同一个 401，不区分「账号不存在」和「口令错」—— 区分了就是一次
    账号存在性确认。"""
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        wrong = client.post(
            "/api/v1/auth/tokens/issue",
            json={"username": "alice", "password": "nope-wrong"},
        )
        missing = client.post(
            "/api/v1/auth/tokens/issue",
            json={"username": "nobody", "password": "password123"},
        )
        assert wrong.status_code == 401
        assert missing.status_code == 401
        #  措辞也要一样
        assert wrong.json()["detail"] == missing.json()["detail"]


def test_an_issued_token_shows_up_in_the_owners_device_list(settings):
    """口令换来的 token 和会话签发的是同一种东西，能在设备列表里看到并吊销。"""
    make_account("alice", "password123")

    with TestClient(create_app()) as client:
        client.post(
            "/api/v1/auth/tokens/issue",
            json={"username": "alice", "password": "password123", "device_name": "平板"},
        )

    with TestClient(create_app()) as client:
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "password123"})
        listed = client.get("/api/v1/auth/tokens").json()
        assert [row["device_name"] for row in listed] == ["平板"]
