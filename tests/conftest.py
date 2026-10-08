import pytest

from funread_api import security


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
