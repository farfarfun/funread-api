"""Shelf, reading progress, and offline chapter cache."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field

from funread.legado.reader import (
    clear_chapter_cache,
    get_shelf_book,
    list_cached_chapter_indexes,
)
from funread_api.security import CurrentUser, require_user

from .deps import DownloadTracker, get_download_tracker, get_reader_service
from .reader import ChapterModel

router = APIRouter(prefix="/shelf", tags=["shelf"])

#: Cap on one download request. The service fetches serially with a pause
#: between chapters, so a 3,000-chapter book would hold a worker for an hour.
#: The client walks the toc in slices instead.
MAX_DOWNLOAD_CHAPTERS = 200


class ShelfBookIn(BaseModel):
    name: str
    author: str = ""
    cover_url: str = ""
    intro: str = ""
    url_id: int | None = None
    book_url: str = ""
    toc_url: str = ""
    last_chapter: str = ""


class ProgressIn(BaseModel):
    chapter_index: int = Field(ge=0)
    chapter_url: str = ""
    chapter_name: str = ""
    char_offset: int = Field(default=0, ge=0)


class ProgressOut(BaseModel):
    chapter_index: int
    chapter_name: str
    char_offset: int


class ShelfBookOut(BaseModel):
    book_key: str
    name: str
    author: str
    cover_url: str
    intro: str
    url_id: int | None
    book_url: str
    toc_url: str
    last_chapter: str
    updated_at: str
    progress: ProgressOut | None


class BookKeyOut(BaseModel):
    book_key: str


class SwitchSourceIn(BaseModel):
    url_id: int
    book_url: str


class DownloadIn(BaseModel):
    url_id: int
    chapters: list[ChapterModel]
    #: Seconds between chapters. Exposed so a test (or a known-friendly source)
    #: can turn the throttle off; the default is the service's polite pace.
    interval: float = Field(default=0.5, ge=0, le=5)


class DownloadAccepted(BaseModel):
    book_key: str
    queued: int
    #: Poll ``GET /shelf/{book_key}/cached`` (or /download/{task_id}) with this.
    task_id: str


class DownloadProgress(BaseModel):
    task_id: str
    state: str
    total: int
    done: int
    failed: int
    detail: str = ""


class CacheState(BaseModel):
    book_key: str
    chapter_indexes: list[int]
    #: The running download for this book, if this process has one. Lets the UI
    #: show a spinner instead of a stale count that silently grows.
    downloading: DownloadProgress | None = None


def _require_shelf_book(book_key: str, user: CurrentUser) -> None:
    """404 rather than 403 when the book belongs to someone else.

    The lookup is already scoped by ``user_id``, so another user's book is
    simply not there -- and saying "forbidden" would confirm it exists.
    """
    service = get_reader_service()
    if get_shelf_book(book_key, user_id=user.user_id, database_url=service.database_url) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="书架里没有这本书")


@router.get("", response_model=list[ShelfBookOut])
def list_books(user: CurrentUser = Depends(require_user)) -> list[ShelfBookOut]:
    return [ShelfBookOut(**item) for item in get_reader_service().shelf(user_id=user.user_id)]


@router.post("", response_model=BookKeyOut, status_code=status.HTTP_201_CREATED)
def add_book(
    payload: ShelfBookIn,
    user: CurrentUser = Depends(require_user),
) -> BookKeyOut:
    """Idempotent: the key is derived from 书名+作者, so re-adding updates."""
    book_key = get_reader_service().add_to_shelf(
        payload.model_dump(exclude_none=True), user_id=user.user_id
    )
    return BookKeyOut(book_key=book_key)


@router.delete("/{book_key}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def remove_book(book_key: str, user: CurrentUser = Depends(require_user)) -> None:
    if not get_reader_service().remove_from_shelf(book_key, user_id=user.user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="书架里没有这本书")


@router.put("/{book_key}/progress", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def save_progress(
    book_key: str,
    payload: ProgressIn,
    user: CurrentUser = Depends(require_user),
) -> None:
    _require_shelf_book(book_key, user)
    get_reader_service().save_progress(
        book_key=book_key, user_id=user.user_id, **payload.model_dump()
    )


@router.post("/{book_key}/source", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def switch_source(
    book_key: str,
    payload: SwitchSourceIn,
    user: CurrentUser = Depends(require_user),
) -> None:
    """Point the book at another source. Drops the chapter cache on purpose:
    chapter numbering differs between sites, so keeping it would mix chapters."""
    _require_shelf_book(book_key, user)
    get_reader_service().switch_source(
        book_key, url_id=payload.url_id, book_url=payload.book_url, user_id=user.user_id
    )


@router.post(
    "/{book_key}/download",
    response_model=DownloadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def download(
    book_key: str,
    payload: DownloadIn,
    tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_user),
) -> DownloadAccepted:
    """Pre-fetch chapters into the offline cache, in the background.

    202 rather than a result: a serial throttled fetch of a few hundred
    chapters outlives any sane request timeout. Poll ``GET /shelf/{key}/cached``
    for progress -- already-cached chapters are skipped, so a client that
    retries after a failure resumes instead of refetching.
    """
    _require_shelf_book(book_key, user)
    if not payload.chapters:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="没有要下载的章节"
        )
    if len(payload.chapters) > MAX_DOWNLOAD_CHAPTERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"一次最多下载 {MAX_DOWNLOAD_CHAPTERS} 章",
        )

    service = get_reader_service()
    tracker = get_download_tracker()
    running = tracker.active_for(book_key, user.user_id)
    if running is not None:
        #  Two concurrent serial fetches of the same book would just hammer the
        #  source twice for the same text. Hand back the task already in flight.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"这本书正在下载中（task_id={running.task_id}）",
        )

    chapters = [chapter.to_chapter() for chapter in payload.chapters]
    task = tracker.start(book_key, user.user_id, len(chapters))

    def _run() -> None:
        try:
            stats = service.download_chapters(
                book_key, payload.url_id, chapters, interval=payload.interval
            )
        except Exception as error:
            #  The task runs after the response; an unhandled error here would
            #  only reach the server log, leaving the UI spinning forever.
            tracker.fail(task.task_id, str(error))
            return
        tracker.finish(task.task_id, stats)

    tasks.add_task(_run)
    return DownloadAccepted(book_key=book_key, queued=len(chapters), task_id=task.task_id)


def _progress_of(tracker: DownloadTracker, task_id: str) -> DownloadProgress | None:
    task = tracker.get(task_id)
    if task is None:
        return None
    return DownloadProgress(
        task_id=task.task_id,
        state=task.state,
        total=task.total,
        done=task.done,
        failed=task.failed,
        detail=task.detail,
    )


@router.get("/{book_key}/download/{task_id}", response_model=DownloadProgress)
def download_progress(
    book_key: str,
    task_id: str,
    user: CurrentUser = Depends(require_user),
) -> DownloadProgress:
    """How one batch download is going.

    404 once the entry is evicted (ten minutes after it finishes) -- by then
    ``GET /cached`` is the answer, and it is the durable one.
    """
    _require_shelf_book(book_key, user)
    task = get_download_tracker().get(task_id)
    if task is None or task.book_key != book_key or task.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这个下载任务")
    return _progress_of(get_download_tracker(), task_id)  # type: ignore[return-value]


@router.get("/{book_key}/cached", response_model=CacheState)
def cached_chapters(book_key: str, user: CurrentUser = Depends(require_user)) -> CacheState:
    """Which chapters are already on disk.

    Gated on the caller having the book on *their* shelf even though the cache
    itself is shared -- otherwise this would answer "which chapters has anyone
    downloaded" for an arbitrary book_key.
    """
    _require_shelf_book(book_key, user)
    service = get_reader_service()
    running = get_download_tracker().active_for(book_key, user.user_id)
    return CacheState(
        book_key=book_key,
        chapter_indexes=list_cached_chapter_indexes(book_key, database_url=service.database_url),
        downloading=_progress_of(get_download_tracker(), running.task_id) if running else None,
    )


@router.delete("/{book_key}/cached", response_model=CacheState)
def clear_cache(book_key: str, user: CurrentUser = Depends(require_user)) -> CacheState:
    """Drop the cached text. Shared, so this affects every reader of the book --
    acceptable because it only costs a refetch, and a stuck bad cache is worse."""
    _require_shelf_book(book_key, user)
    service = get_reader_service()
    clear_chapter_cache(book_key, database_url=service.database_url)
    return CacheState(book_key=book_key, chapter_indexes=[])
