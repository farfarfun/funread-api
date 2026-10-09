"""Reader flow: aggregated search → book detail → table of contents → chapter text."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from funread.legado.engine import BookInfo, Chapter
from funread.legado.reader import DEFAULT_SEARCH_SOURCES
from funread_api.security import CurrentUser, require_user

from .deps import engine_errors, get_reader_service

router = APIRouter(prefix="/reader", tags=["reader"])


class SourceRef(BaseModel):
    url_id: int
    source_name: str
    book_url: str


class SearchBookOut(BaseModel):
    book_key: str
    name: str
    author: str
    cover_url: str
    intro: str
    kind: str
    word_count: str
    last_chapter: str
    sources: list[SourceRef]


class SearchPage(BaseModel):
    items: list[SearchBookOut]
    total: int
    limit: int
    offset: int
    #: How the fan-out went this round. Without these the UI cannot tell
    #: "nothing matched" from "nine of ten sources need JS", and both look
    #: like a bug to the person holding the phone.
    sources_tried: int
    sources_ok: int
    #: Sources skipped because their rules need a JS runtime. Structural --
    #: a different keyword will not help, so the UI should say so.
    js_skipped: int
    failed: int


class BookInfoModel(BaseModel):
    """Both the detail response and the input to /toc and /content.

    It round-trips because of ``variables``: rules capture values with ``@put``
    on the detail page and read them back with ``@get`` on the toc/content
    pages. Dropping them between requests silently breaks those sources.
    """

    name: str = ""
    author: str = ""
    book_url: str = ""
    toc_url: str = ""
    kind: str = ""
    word_count: str = ""
    last_chapter: str = ""
    intro: str = ""
    cover_url: str = ""
    source_url: str = ""
    source_name: str = ""
    can_rename: bool = False
    variables: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def of(cls, info: BookInfo) -> "BookInfoModel":
        return cls(**asdict(info))

    def to_info(self) -> BookInfo:
        return BookInfo(**self.model_dump())


class ChapterModel(BaseModel):
    index: int = 0
    name: str = ""
    url: str = ""
    update_time: str = ""
    is_vip: bool = False
    is_pay: bool = False
    variables: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def of(cls, chapter: Chapter) -> "ChapterModel":
        return cls(**asdict(chapter))

    def to_chapter(self) -> Chapter:
        return Chapter(**self.model_dump())


class TocRequest(BaseModel):
    url_id: int
    book: BookInfoModel


class TocOut(BaseModel):
    items: list[ChapterModel]
    total: int


class ContentRequest(BaseModel):
    url_id: int
    chapter: ChapterModel
    book: BookInfoModel | None = None
    #: When given, the chapter is served from (and written to) the offline cache.
    book_key: str = ""


class ContentOut(BaseModel):
    title: str
    text: str
    url: str
    next_url: str


class ScanReport(BaseModel):
    scanned: int
    complete: int
    needs_js: int
    enabled: int


@router.get("/search", response_model=SearchPage)
def search(
    keyword: str = Query(min_length=1, max_length=64),
    max_sources: int = Query(default=DEFAULT_SEARCH_SOURCES, ge=1, le=40),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SearchPage:
    """Search several sources at once and merge hits by 书名+作者.

    ``offset`` slices *this* call's result set. Every call re-runs a live
    fan-out, so page 2 is not guaranteed to continue page 1 -- a source that
    timed out the first time may answer the second. Ordering is deterministic
    given the same source responses (most sources first), which makes it good
    enough for "load more" and wrong for deep paging. ``total`` is the size of
    this round, so the client can stop asking.
    """
    report = get_reader_service().search_report(keyword, max_sources=max_sources)
    window = report["items"][offset : offset + limit]
    return SearchPage(
        items=[SearchBookOut(**item) for item in window],
        total=report["total"],
        limit=limit,
        offset=offset,
        sources_tried=report["sources_tried"],
        sources_ok=report["sources_ok"],
        js_skipped=report["js_skipped"],
        failed=report["failed"],
    )


@router.get("/book", response_model=BookInfoModel)
def book_info(
    url_id: int,
    book_url: str = Query(min_length=1),
    name: str = "",
    author: str = "",
) -> BookInfoModel:
    with engine_errors():
        info = get_reader_service().book_info(url_id, book_url, name=name, author=author)
    return BookInfoModel.of(info)


@router.post("/toc", response_model=TocOut)
def toc(payload: TocRequest) -> TocOut:
    """POST, not GET: the engine needs the whole ``BookInfo`` (see BookInfoModel)."""
    with engine_errors():
        chapters = get_reader_service().toc(payload.url_id, payload.book.to_info())
    items = [ChapterModel.of(chapter) for chapter in chapters]
    return TocOut(items=items, total=len(items))


@router.post("/content", response_model=ContentOut)
def content(payload: ContentRequest) -> ContentOut:
    with engine_errors():
        result = get_reader_service().content(
            payload.url_id,
            payload.chapter.to_chapter(),
            info=payload.book.to_info() if payload.book else None,
            book_key=payload.book_key,
        )
    #  `pages` is left out on purpose: it is the un-merged per-page source text,
    #  a debugging aid that would roughly double the payload on every chapter.
    return ContentOut(
        title=result.title or payload.chapter.name,
        text=result.text,
        url=result.url,
        next_url=result.next_url,
    )


@router.get("/sources", response_model=list[SourceRef])
def sources_for(
    book_key: str = Query(min_length=1),
    user: CurrentUser = Depends(require_user),
) -> list[SourceRef]:
    """换源 list: which other sources carry this shelf book.

    Reads the caller's shelf to recover the title, so it needs an identity even
    though the search itself is stateless.
    """
    return [
        SourceRef(**item)
        for item in get_reader_service().sources_for(book_key, user_id=user.user_id)
    ]


@router.post("/scan", response_model=ScanReport, status_code=status.HTTP_200_OK)
def scan(limit: int | None = Query(default=None, ge=1)) -> ScanReport:
    """Rebuild the candidate pool from the local source archive.

    Required before the first search -- until this runs, ``reader_source_prefs``
    is empty and every search returns nothing. Synchronous and slow (tens of
    thousands of small files); ``limit`` exists so a smoke run stays quick.

    Safe to re-run: it only refreshes the static verdicts and leaves the
    live-result columns (``fail_count``/``last_ok_at``) alone.
    """
    return ScanReport(**get_reader_service().registry.scan(limit=limit))
