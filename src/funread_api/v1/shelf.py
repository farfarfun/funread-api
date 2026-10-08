"""Shelf, reading progress, and offline chapter cache."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field

from funread.legado.reader import (
    clear_chapter_cache,
    get_shelf_book,
    list_cached_chapter_indexes,
)

from .deps import get_reader_service
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


class CacheState(BaseModel):
    book_key: str
    chapter_indexes: list[int]


def _require_shelf_book(book_key: str) -> None:
    service = get_reader_service()
    if get_shelf_book(book_key, database_url=service.database_url) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="书架里没有这本书")


@router.get("", response_model=list[ShelfBookOut])
def list_books() -> list[ShelfBookOut]:
    return [ShelfBookOut(**item) for item in get_reader_service().shelf()]


@router.post("", response_model=BookKeyOut, status_code=status.HTTP_201_CREATED)
def add_book(payload: ShelfBookIn) -> BookKeyOut:
    """Idempotent: the key is derived from 书名+作者, so re-adding updates."""
    book_key = get_reader_service().add_to_shelf(payload.model_dump(exclude_none=True))
    return BookKeyOut(book_key=book_key)


@router.delete("/{book_key}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def remove_book(book_key: str) -> None:
    if not get_reader_service().remove_from_shelf(book_key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="书架里没有这本书")


@router.put("/{book_key}/progress", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def save_progress(book_key: str, payload: ProgressIn) -> None:
    _require_shelf_book(book_key)
    get_reader_service().save_progress(book_key=book_key, **payload.model_dump())


@router.post("/{book_key}/source", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def switch_source(book_key: str, payload: SwitchSourceIn) -> None:
    """Point the book at another source. Drops the chapter cache on purpose:
    chapter numbering differs between sites, so keeping it would mix chapters."""
    _require_shelf_book(book_key)
    get_reader_service().switch_source(book_key, url_id=payload.url_id, book_url=payload.book_url)


@router.post(
    "/{book_key}/download",
    response_model=DownloadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def download(book_key: str, payload: DownloadIn, tasks: BackgroundTasks) -> DownloadAccepted:
    """Pre-fetch chapters into the offline cache, in the background.

    202 rather than a result: a serial throttled fetch of a few hundred
    chapters outlives any sane request timeout. Poll ``GET /shelf/{key}/cached``
    for progress -- already-cached chapters are skipped, so a client that
    retries after a failure resumes instead of refetching.
    """
    _require_shelf_book(book_key)
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
    chapters = [chapter.to_chapter() for chapter in payload.chapters]
    tasks.add_task(
        service.download_chapters,
        book_key,
        payload.url_id,
        chapters,
        interval=payload.interval,
    )
    return DownloadAccepted(book_key=book_key, queued=len(chapters))


@router.get("/{book_key}/cached", response_model=CacheState)
def cached_chapters(book_key: str) -> CacheState:
    service = get_reader_service()
    return CacheState(
        book_key=book_key,
        chapter_indexes=list_cached_chapter_indexes(book_key, database_url=service.database_url),
    )


@router.delete("/{book_key}/cached", response_model=CacheState)
def clear_cache(book_key: str) -> CacheState:
    service = get_reader_service()
    clear_chapter_cache(book_key, database_url=service.database_url)
    return CacheState(book_key=book_key, chapter_indexes=[])
