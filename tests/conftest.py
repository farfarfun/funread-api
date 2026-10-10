import asyncio

import pytest

from funread.legado.reader import storage
from funread_api import accounts as accounts_module
from funread_api import security
from funread_api.v1.deps import get_download_tracker, reset_reader_services


@pytest.fixture(autouse=True)
def _no_ambient_secrets(monkeypatch):
    """Cut the tests off from the machine's funsecret store.

    funsecret is a real per-developer store -- on this machine it holds a
    production MySQL URL -- so leaving it live would make "no password
    configured" mean "no password configured on this laptop", and would let
    a locally-set secret turn unrelated tests into 401s. Tests that want auth
    on set FUNREAD_API_PASSWORD explicitly.
    """
    monkeypatch.setattr(security, "_read_secret", lambda: None)
    monkeypatch.delenv("FUNREAD_API_PASSWORD", raising=False)
    monkeypatch.delenv("FUNREAD_READER_PUBLIC", raising=False)
    #  Registration is open by default now that the gate is an invite-code row
    #  rather than a static env secret; a developer's shell must not close it
    #  (nor open it) behind the tests' back.
    monkeypatch.delenv("FUNREAD_REGISTER_OPEN", raising=False)
    #  A fixed session secret: the real resolver would otherwise read funsecret
    #  or *write* a key file into the developer's ~/.config.
    monkeypatch.setenv("FUNREAD_SESSION_SECRET", "test-session-secret")


@pytest.fixture(autouse=True)
def _isolate_database(tmp_path, monkeypatch):
    """Point every test at its own SQLite file and cache root.

    Belt and braces over funread's own autouse fixture: this process resolves
    the database through ``funread.base.config``, which falls back to funsecret
    -- and on this machine that is the production MySQL. A test that forgets to
    set the env var must not reach it.

    ``_INITIALIZED_DATABASES`` and the memoised services are keyed by URL, so
    both are cleared: otherwise the first test's engine (and its schema
    migration state) would be reused by every later one.
    """
    monkeypatch.setenv("FUNREAD_DATABASE_URL", f"sqlite:///{tmp_path / 'funread-test.db'}")
    monkeypatch.setenv("FUNREAD_CACHE_ROOT", str(tmp_path / "hubs"))
    monkeypatch.setattr(storage, "_INITIALIZED_DATABASES", set())
    reset_reader_services()
    get_download_tracker().clear()
    yield
    reset_reader_services()
    get_download_tracker().clear()
    #  The account engine is async and cached by URL like the others, but it
    #  also has to be *disposed*: aiosqlite puts every connection on its own
    #  thread, and 600-odd tests each leaking one ends the run with
    #  "cannot schedule new futures after shutdown".
    asyncio.run(accounts_module.reset_async_engines())
