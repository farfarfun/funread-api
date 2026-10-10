"""The account layer itself: URL translation and the pre-funauth table migration.

Both are startup-time code that only runs on an *upgrade*, so nothing else in
the suite exercises them -- and a mistake here loses people's accounts.
"""

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from accounts_support import legacy_scrypt_hash
from fastapi.testclient import TestClient

from funread.legado.manage.source.storage import _get_engine
from funread.legado.reader import storage
from funread_api.accounts import ReaderUser, init_auth_db, to_async_url
from funread_api.app import create_app


@pytest.fixture
def env(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path / 'accounts.db'}"
    monkeypatch.setenv("FUNREAD_DATABASE_URL", url)
    monkeypatch.setenv("FUNREAD_CACHE_ROOT", str(tmp_path / "hubs"))
    monkeypatch.setattr(storage, "_INITIALIZED_DATABASES", set())
    return url


# ------------------------------------------------------------------ URL 翻译


@pytest.mark.parametrize(
    ("sync_url", "expected"),
    [
        ("sqlite:///x.db", "sqlite+aiosqlite:///x.db"),
        ("sqlite+pysqlite:///x.db", "sqlite+aiosqlite:///x.db"),
        ("mysql+pymysql://u:p@h/db", "mysql+aiomysql://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
    ],
)
def test_sync_urls_are_translated_to_their_async_driver(sync_url, expected):
    assert to_async_url(sync_url).replace("***", "p") == expected


def test_an_already_async_url_is_left_alone():
    """The resolved URL may already be async if the operator configured it that way."""
    assert to_async_url("sqlite+aiosqlite:///x.db") == "sqlite+aiosqlite:///x.db"


def test_a_driverless_url_is_translated_regardless_of_sqlalchemy_version():
    """``postgresql://`` must not hinge on which driver SQLAlchemy defaults to.

    2.0 defaults it to psycopg2 (sync), 2.1 to psycopg3 (async-capable). Asking
    SQLAlchemy to name the driver would make the answer version-dependent; only
    a driver written *in* the URL counts as already-async.
    """
    assert to_async_url("postgresql://h/db") == "postgresql+asyncpg://h/db"
    assert to_async_url("postgresql+psycopg://h/db") == "postgresql+psycopg://h/db"


def test_a_driver_with_no_async_equivalent_fails_loudly():
    """At startup, with the driver named -- not as a 500 on someone's first login."""
    with pytest.raises(ValueError, match="没有可用的异步驱动"):
        to_async_url("mssql+pyodbc://u:p@h/db")


# ------------------------------------------------------------------ 旧表迁移


def _create_pre_funauth_user_table(url, rows):
    """Rebuild the M3d shape: ``user_id`` PK, ``disabled`` instead of ``is_active``."""
    engine = _get_engine(url)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE IF EXISTS reader_user"))
        connection.execute(
            sa.text(
                "CREATE TABLE reader_user ("
                " user_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
                " username VARCHAR(64) NOT NULL UNIQUE,"
                " password_hash VARCHAR(255) NOT NULL,"
                " disabled BOOLEAN NOT NULL DEFAULT 0,"
                " created_at DATETIME NOT NULL,"
                " updated_at DATETIME NOT NULL)"
            )
        )
        for user_id, username, password_hash, disabled in rows:
            connection.execute(
                sa.text(
                    "INSERT INTO reader_user"
                    " (user_id, username, password_hash, disabled, created_at, updated_at)"
                    " VALUES (:i, :u, :h, :d,"
                    "         '2026-10-01 00:00:00', '2026-10-01 00:00:00')"
                ),
                {"i": user_id, "u": username, "h": password_hash, "d": disabled},
            )


def _read_users(url):
    engine = _get_engine(url)
    with engine.connect() as connection:
        return {
            row.username: row
            for row in connection.execute(
                sa.text("SELECT id, username, password_hash, role, is_active FROM reader_user")
            )
        }


def test_the_legacy_table_is_migrated_in_place(env):
    _create_pre_funauth_user_table(
        env,
        [
            (3, "alice", legacy_scrypt_hash("password123"), 0),
            (7, "bob", legacy_scrypt_hash("password456"), 1),
        ],
    )

    init_auth_db(env)

    users = _read_users(env)
    #  The ids carry over untouched -- which is the whole reason the shelf and
    #  progress tables need no migration of their own.
    assert users["alice"].id == 3
    assert users["bob"].id == 7
    #  disabled -> is_active, inverted
    assert bool(users["alice"].is_active) is True
    assert bool(users["bob"].is_active) is False
    #  Admin is never granted by a migration; it has to be given deliberately.
    assert users["alice"].role == "guest"
    #  The scrypt hash is copied as-is: login upgrades it, nobody resets anything.
    assert users["alice"].password_hash.startswith("scrypt$")


def test_the_legacy_table_is_dropped_so_there_is_one_truth(env):
    _create_pre_funauth_user_table(env, [(1, "alice", legacy_scrypt_hash("password123"), 0)])

    init_auth_db(env)

    engine = _get_engine(env)
    assert "reader_user__pre_funauth" not in set(sa.inspect(engine).get_table_names())


def test_migrating_twice_is_a_no_op(env):
    """A restart must not re-run it -- the second pass would find no ``user_id``."""
    _create_pre_funauth_user_table(env, [(1, "alice", legacy_scrypt_hash("password123"), 0)])
    init_auth_db(env)

    import funread_api.accounts as accounts_module

    accounts_module._AUTH_INITIALIZED.clear()
    init_auth_db(env)

    assert list(_read_users(env)) == ["alice"]


def test_a_fresh_database_just_gets_the_tables(env):
    init_auth_db(env)

    tables = set(sa.inspect(_get_engine(env)).get_table_names())
    assert {"reader_user", "reader_invite_code"} <= tables
    assert _read_users(env) == {}


def test_an_already_migrated_database_is_not_touched(env):
    init_auth_db(env)
    engine = _get_engine(env)
    with engine.begin() as connection:
        connection.execute(
            ReaderUser.__table__.insert().values(
                id=5,
                username="alice",
                password_hash="bcrypt-ish",
                role="admin",
                is_active=True,
                created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            )
        )

    import funread_api.accounts as accounts_module

    accounts_module._AUTH_INITIALIZED.clear()
    init_auth_db(env)

    assert _read_users(env)["alice"].role == "admin"


def test_the_whole_upgrade_path_ends_in_a_working_login(env):
    """Legacy table + legacy hash + a shelf, all the way to a logged-in request."""
    storage.init_reader_db(env)
    storage.upsert_shelf_book({"name": "剑来"}, user_id=3, database_url=env)
    _create_pre_funauth_user_table(env, [(3, "alice", legacy_scrypt_hash("password123"), 0)])

    with TestClient(create_app()) as client:
        login = client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "password123"}
        )
        assert login.status_code == 200
        assert login.json()["user_id"] == 3

        #  The shelf was keyed by the old user_id and is still hers
        assert [book["name"] for book in client.get("/api/v1/shelf").json()] == ["剑来"]

    assert not _read_users(env)["alice"].password_hash.startswith("scrypt$")
