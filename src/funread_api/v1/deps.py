"""Shared ``ReaderService`` instance plus the engine-error → HTTP mapping."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Tuple

from fastapi import HTTPException, status

from funread.legado.engine import (
    JsNotSupportedError,
    UnsupportedFeatureError,
    WebViewNotSupportedError,
)
from funread.legado.reader import ReaderService

_lock = threading.Lock()
_services: Dict[Tuple[Optional[str], Optional[str]], ReaderService] = {}


def get_reader_service() -> ReaderService:
    """Process-wide service, keyed by the env vars that configure it.

    Keyed instead of a plain singleton because the tests monkeypatch
    ``FUNREAD_CACHE_ROOT``/``FUNREAD_DATABASE_URL`` per test, and a bare
    singleton would leak the first test's tmp_path into every later one. In
    production both are either unset or constant, so this is one instance.

    Sharing matters: ``SourceRegistry`` memoises parsed ``SourceSpec``s, and an
    aggregated search touches a dozen of them per request.
    """
    key = (os.environ.get("FUNREAD_CACHE_ROOT"), os.environ.get("FUNREAD_DATABASE_URL"))
    with _lock:
        service = _services.get(key)
        if service is None:
            #  Passing the raw env values through (``None`` included) keeps
            #  funread's own resolve_* fallbacks in charge.
            service = ReaderService(cache_root=key[0], database_url=key[1])
            _services[key] = service
        return service


def reset_reader_services() -> None:
    """Drop the cached services. Only for tests and hot config reloads."""
    with _lock:
        _services.clear()


@contextmanager
def engine_errors() -> Iterator[None]:
    """Turn engine exceptions into HTTP responses.

    The distinction that matters to the UI: 422 means *this source will never
    work* (phase 1 can't run its JS / webView rules, so offer 换源), while 502
    means *the site misbehaved this time* (so offer retry).
    """
    try:
        yield
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (JsNotSupportedError, UnsupportedFeatureError, WebViewNotSupportedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"此源暂不支持：{exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"源站抓取失败：{exc}",
        ) from exc


__all__ = ["engine_errors", "get_reader_service", "reset_reader_services"]
