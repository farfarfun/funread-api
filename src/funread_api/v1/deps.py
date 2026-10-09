"""Shared ``ReaderService`` instance plus the engine-error → HTTP mapping."""

from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, Optional, Tuple

from fastapi import HTTPException, status

from funread.legado.engine import (
    JsNotSupportedError,
    UnsupportedFeatureError,
    WebViewNotSupportedError,
)
from funread.legado.reader import ReaderService, RssService

_lock = threading.Lock()
_services: Dict[Tuple[Optional[str], Optional[str]], ReaderService] = {}
_rss_services: Dict[Tuple[Optional[str], Optional[str]], RssService] = {}


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


def get_rss_service() -> RssService:
    """Process-wide subscription service, keyed the same way as the reader one.

    A separate instance rather than a field on ``ReaderService``: it owns an
    RSS-typed ``SourceRegistry`` (different archive subdirectory, different
    ``reader_source_prefs`` partition), and sharing one registry between book
    and rss would hand book specs to the RSS engine.
    """
    key = (os.environ.get("FUNREAD_CACHE_ROOT"), os.environ.get("FUNREAD_DATABASE_URL"))
    with _lock:
        service = _rss_services.get(key)
        if service is None:
            service = RssService(cache_root=key[0], database_url=key[1])
            _rss_services[key] = service
        return service


def reset_reader_services() -> None:
    """Drop the cached services. Only for tests and hot config reloads."""
    with _lock:
        _services.clear()
        _rss_services.clear()


@dataclass
class DownloadTask:
    """One in-flight batch download."""

    task_id: str
    book_key: str
    user_id: int
    total: int
    done: int = 0
    failed: int = 0
    #: ``running`` | ``done`` | ``error``
    state: str = "running"
    detail: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


class DownloadTracker:
    """Which batch downloads are running right now.

    In-process and deliberately so: it answers "is something fetching this book
    at the moment", which is a property of *this* process. The authoritative
    answer to "which chapters do we have" is ``reader_chapter_cache`` in the
    database, and the UI reads that separately -- so losing this registry on
    restart costs a spinner, not data.

    Consequence worth knowing: with more than one uvicorn worker a download
    started in worker A is invisible to worker B. The service runs
    single-process, and the DB-backed chapter list still converges either way.
    """

    #: Finished entries are kept this long so a client that polls after the
    #: download ends still sees how it went instead of a bare 404.
    RETENTION_SECONDS = 600
    #: Cap on remembered entries, so a long-lived process cannot grow forever.
    MAX_ENTRIES = 256

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, DownloadTask] = {}

    def _evict(self) -> None:
        cutoff = time.time() - self.RETENTION_SECONDS
        stale = [
            key
            for key, task in self._tasks.items()
            if task.finished_at is not None and task.finished_at < cutoff
        ]
        for key in stale:
            del self._tasks[key]
        if len(self._tasks) > self.MAX_ENTRIES:
            finished = sorted(
                (task for task in self._tasks.values() if task.finished_at is not None),
                key=lambda task: task.finished_at or 0.0,
            )
            for task in finished[: len(self._tasks) - self.MAX_ENTRIES]:
                self._tasks.pop(task.task_id, None)

    def start(self, book_key: str, user_id: int, total: int) -> DownloadTask:
        task = DownloadTask(
            task_id=uuid.uuid4().hex, book_key=book_key, user_id=user_id, total=total
        )
        with self._lock:
            self._evict()
            self._tasks[task.task_id] = task
        return task

    def finish(self, task_id: str, stats: Dict[str, int]) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.done = int(stats.get("downloaded", 0)) + int(stats.get("cached", 0))
            task.failed = int(stats.get("failed", 0))
            task.state = "done"
            task.finished_at = time.time()

    def fail(self, task_id: str, detail: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.state = "error"
            task.detail = detail[:500]
            task.finished_at = time.time()

    def get(self, task_id: str) -> Optional[DownloadTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def active_for(self, book_key: str, user_id: int) -> Optional[DownloadTask]:
        """The running task for this user's copy of the book, if any."""
        with self._lock:
            for task in self._tasks.values():
                if (
                    task.book_key == book_key
                    and task.user_id == user_id
                    and task.state == "running"
                ):
                    return task
        return None

    def clear(self) -> None:
        """Only for tests."""
        with self._lock:
            self._tasks.clear()


_download_tracker = DownloadTracker()


def get_download_tracker() -> DownloadTracker:
    return _download_tracker


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


__all__ = [
    "DownloadTask",
    "DownloadTracker",
    "engine_errors",
    "get_download_tracker",
    "get_reader_service",
    "get_rss_service",
    "reset_reader_services",
]
