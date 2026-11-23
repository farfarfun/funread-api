"""Casdoor identity, schema upgrade, and authorization boundaries."""

from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import sqlalchemy as sa
from accounts_support import make_account
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from funauth import UserRole

from funread.base import config
from funread.legado.manage.source.storage import _get_engine
from funread_api.app import create_app
from funread_api.v1 import sso
from funread_api.v1.sso import load_config


class FakeOidcClient:
    claims = {
        "iss": "https://id.example.com",
        "sub": "reader-sub",
        "preferred_username": "reader",
    }

    async def authorize_redirect(self, request, redirect_uri: str) -> RedirectResponse:
        assert redirect_uri == "https://read.example.com/api/v1/auth/oidc/callback"
        return RedirectResponse("https://id.example.com/authorize")

    async def authorize_access_token(self, request) -> dict:
        return {"id_token": "validated", "userinfo": self.claims}


def _configure() -> None:
    config.set_overrides(
        oidc_issuer="https://id.example.com",
        oidc_client_id="funread",
        oidc_client_secret="secret",
        oidc_redirect_uri="https://read.example.com/api/v1/auth/oidc/callback",
        oidc_admin_subject="admin-sub",
    )


def test_oidc_login_creates_account_and_admin_is_only_matching_sub():
    _configure()
    app = create_app()
    fake = FakeOidcClient()
    app.state.oidc_client = fake
    with TestClient(app, base_url="https://read.example.com") as client:
        assert client.get("/api/v1/auth/me").json()["authenticated"] is False
        assert (
            client.post("/api/v1/auth/login", json={"username": "x", "password": "x"}).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/auth/register", json={"username": "x", "password": "x"}
            ).status_code
            == 404
        )
        assert client.post("/api/v1/auth/admin/login", json={"password": "x"}).status_code == 404
        assert (
            client.post(
                "/api/v1/auth/tokens/issue", json={"username": "x", "password": "x"}
            ).status_code
            == 404
        )
        assert client.get("/api/v1/reader/search", params={"keyword": "book"}).status_code == 401
        assert client.get("/api/v1/auth/oidc/config").json() == {"enabled": True}

        client.get("/api/v1/auth/oidc/login?next=/web/account")
        callback = client.get("/api/v1/auth/oidc/callback", follow_redirects=False)
        assert callback.status_code == 303
        assert callback.headers["location"] == "/web/account"
        state = client.get("/api/v1/auth/me").json()
        assert state["oidc_enabled"] is True
        assert state["role"] == "guest"
        assert state["username"].startswith("sso_reader_")
        assert client.get("/api/v1/sources").status_code == 403
        created = client.post("/api/v1/auth/tokens", json={"device_name": "phone"}).json()
        assert created["token"]
        current = client.get(
            "/api/v1/auth/tokens/current",
            headers={"Authorization": f"Bearer {created['token']}"},
        )
        assert current.json()["id"] == created["id"]
        assert current.json()["username"] == state["username"]
        assert (
            client.get(
                "/api/v1/auth/tokens/current", headers={"Authorization": "Bearer invalid"}
            ).status_code
            == 401
        )

        client.get("/api/v1/auth/oidc/login?next=https://evil.example.com")
        assert (
            client.get("/api/v1/auth/oidc/callback", follow_redirects=False).headers["location"]
            == "/web"
        )
        assert client.get("/api/v1/auth/me").json()["user_id"] == state["user_id"]

        fake.claims = {"iss": "https://id.example.com", "sub": "admin-sub"}
        client.get("/api/v1/auth/oidc/login?next=/admin/sources")
        assert (
            client.get("/api/v1/auth/oidc/callback", follow_redirects=False).headers["location"]
            == "/admin/sources"
        )
        assert client.get("/api/v1/auth/me").json()["role"] == "admin"
        assert client.get("/api/v1/sources").status_code == 200
        assert (
            client.patch(
                f"/api/v1/auth/users/{state['user_id']}", json={"role": "admin"}
            ).status_code
            == 400
        )

        engine = _get_engine()
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE reader_user SET role = 'admin' WHERE id = :id"),
                {"id": state["user_id"]},
            )
        assert (
            next(
                row
                for row in client.get("/api/v1/auth/users").json()
                if row["user_id"] == state["user_id"]
            )["role"]
            == "guest"
        )
        fake.claims = {"iss": "https://id.example.com", "sub": "reader-sub"}
        client.get("/api/v1/auth/oidc/login")
        client.get("/api/v1/auth/oidc/callback", follow_redirects=False)
        assert client.get("/api/v1/auth/me").json()["role"] == "guest"
        assert client.get("/api/v1/sources").status_code == 403


def test_existing_account_table_gets_identity_columns():
    engine = _get_engine()
    metadata = sa.MetaData()
    sa.Table(
        "reader_user",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("password_hash", sa.String(128)),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    metadata.create_all(engine)
    _configure()
    with TestClient(create_app(), base_url="https://read.example.com"):
        pass
    inspector = sa.inspect(engine)
    assert {"oidc_issuer", "oidc_subject"} <= {
        column["name"] for column in inspector.get_columns("reader_user")
    }
    assert "uq_reader_user_oidc_identity" in {
        index["name"] for index in inspector.get_indexes("reader_user")
    }


def test_oidc_redirect_uses_pkce_nonce_and_secure_cookie():
    _configure()
    app = create_app()
    app.state.oidc_client.load_server_metadata = AsyncMock(
        return_value={"authorization_endpoint": "https://id.example.com/login/oauth/authorize"}
    )
    with TestClient(app, base_url="https://read.example.com") as client:
        response = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert response.status_code == 302
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]
    assert params["nonce"][0]
    assert params["state"][0]
    assert "secure" in response.headers["set-cookie"].lower()


def test_oidc_rejects_wrong_issuer_and_disabled_user():
    _configure()
    app = create_app()
    fake = FakeOidcClient()
    app.state.oidc_client = fake
    with TestClient(app, base_url="https://read.example.com") as client:
        fake.claims = {"iss": "https://other.example.com", "sub": "reader-sub"}
        assert client.get("/api/v1/auth/oidc/callback").status_code == 400
        fake.claims = {"iss": "https://id.example.com", "sub": "reader-sub"}
        assert client.get("/api/v1/auth/oidc/callback", follow_redirects=False).status_code == 303
        with _get_engine().begin() as connection:
            connection.execute(sa.text("UPDATE reader_user SET is_active = 0"))
        assert client.get("/api/v1/auth/oidc/callback").status_code == 403
        assert client.get("/api/v1/auth/me").json()["authenticated"] is False


def test_existing_local_admin_session_and_device_cannot_bypass_oidc():
    make_account("local-admin", role=UserRole.ADMIN)
    app = create_app()
    with TestClient(app, base_url="https://read.example.com") as client:
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "local-admin", "password": "password123"}
            ).status_code
            == 200
        )
        token = client.post("/api/v1/auth/tokens", json={"device_name": "old"}).json()["token"]
        assert client.get("/api/v1/sources").status_code == 200

        _configure()
        sso.configure(app)
        assert client.get("/api/v1/auth/me").json()["authenticated"] is False
        assert client.get("/api/v1/sources").status_code == 401
        assert (
            client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()[
                "authenticated"
            ]
            is False
        )


def test_incomplete_oidc_config_fails_closed():
    config.set_overrides(oidc_issuer="https://id.example.com")
    try:
        load_config()
    except RuntimeError as error:
        assert "必须全部配置" in str(error)
    else:
        raise AssertionError("partial OIDC config was accepted")
