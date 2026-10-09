"""Reader flow: aggregated search → book detail → table of contents → chapter text."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from funread.legado.engine import BookInfo, Chapter
from funread.legado.reader import DEFAULT_SEARCH_SOURCES
from funread_api.security import CurrentUser, require_user

from .deps import engine_errors, get_reader_service, get_rss_service

router = APIRouter(prefix="/reader", tags=["reader"])


class SearchStats(BaseModel):
    """How the fan-out went this round.

    Without these the UI cannot tell "nothing matched" from "the sources we
    tried were all dead", and both look like a bug to the person holding the
    phone. The pool has 5,606 usable book sources and only single-digit
    percentages actually work, so "tried 24, two answered" is the normal case
    and the UI has to be able to say that out loud.
    """

    sources_tried: int
    sources_ok: int
    #: Sources that returned at least one result. The search stops once there
    #: are enough of these.
    hits: int
    #: Sources skipped because their rules need a JS runtime. Structural --
    #: a different keyword will not help, so the UI should say so.
    js_skipped: int
    failed: int
    #: How many waves of concurrent requests it took.
    waves: int
    elapsed: float
    #: True when the candidate pool really ran out. Only then is "not found" a
    #: settled conclusion rather than "we have not looked that deep yet".
    exhausted: bool
    #: `enough` | `budget` | `max_sources` | `exhausted` | `empty`
    stopped_by: str


class SourceRef(BaseModel):
    url_id: int
    source_name: str
    book_url: str


class SwitchCandidate(SourceRef):
    """换源列表里的一项：一个源 + 这本书在该源下的样子。"""

    name: str
    author: str
    last_chapter: str
    #: True when 书名与作者都对得上（`book_key` 完全一致）。为 False 的那些**不该
    #: 被藏起来** —— 作者名写法差异很常见，而那些源往往恰恰是还活着的。
    exact: bool
    #: 当前正在读的那个源。
    current: bool


class SwitchSourcePage(SearchStats):
    items: list[SwitchCandidate]
    total: int
    book_key: str
    name: str


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


class SearchPage(SearchStats):
    items: list[SearchBookOut]
    total: int
    limit: int
    offset: int


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


class ExploreSourceOut(BaseModel):
    url_id: int
    name: str
    #: 分类名，只给当前这一页补（要读源文件）。
    kinds: list[str] = Field(default_factory=list)


class ExploreSourcePage(BaseModel):
    items: list[ExploreSourceOut]
    total: int
    limit: int
    offset: int


class ExploreKindOut(BaseModel):
    name: str
    #: 源里**原样声明**的串，不是绝对地址。当**不透明令牌**原样回传给
    #: `GET /reader/explore` —— 它可能带 URL 选项（`,{"method":"POST"}`），
    #: 引擎会先拆选项再拼 base_url，自己拼可能把选项弄坏。
    url: str


class ExplorePage(BaseModel):
    items: list[SearchBookOut]
    total: int
    page: int


class ScanReport(BaseModel):
    source_type: str
    scanned: int
    complete: int
    needs_js: int
    #: Sources that need a browser (`singleUrl`). Only non-zero for rss.
    web_view: int
    #: Enabled sources that can also be browsed by category.
    has_explore: int
    enabled: int


@router.get("/search", response_model=SearchPage)
def search(
    keyword: str = Query(min_length=1, max_length=64),
    #  上限对齐服务层的默认值。不要往下收：书源池有 5,606 个可用源，而实跑
    #  可用率只有个位数百分比 —— 卡在几十个源上就是「这本书搜不到」。
    #  真正护住响应时间的是服务层的墙钟预算，不是这个数。
    max_sources: int = Query(default=DEFAULT_SEARCH_SOURCES, ge=1, le=2000),
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
        **{field: report[field] for field in SearchStats.model_fields},
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


@router.get("/sources", response_model=SwitchSourcePage)
def sources_for(
    book_key: str = Query(min_length=1),
    user: CurrentUser = Depends(require_user),
) -> SwitchSourcePage:
    """换源 list: which other sources carry this shelf book.

    This runs a **live aggregated search** by title -- the shelf only records
    the source currently being read, so the real list of alternatives cannot
    come from the database.

    Matching is by **title, loosely**, not by exact ``book_key``. ``book_key``
    is ``md5(title\nauthor)``, so any difference in how a source spells the
    author -- blank, traditional characters, a trailing "（著）" -- produces a
    different key and the source would be dropped silently. Those are exactly
    the sources a reader needs when the current one dies. ``exact`` flags the
    ones where title *and* author agree; the UI may sort those first but must
    not hide the rest.

    Carries the search stats for the same reason ``/search`` does: the UI has
    to be able to distinguish "there really is no other source" from "the
    sources we tried this round were all dead".

    Reads the caller's shelf to recover the title, so it needs an identity even
    though the search itself is stateless.
    """
    with engine_errors():
        report = get_reader_service().sources_for(book_key, user_id=user.user_id)
    return SwitchSourcePage(
        items=[SwitchCandidate(**item) for item in report["items"]],
        total=report["total"],
        book_key=report["book_key"],
        name=report["name"],
        **{field: report[field] for field in SearchStats.model_fields},
    )


@router.get("/explore/sources", response_model=ExploreSourcePage)
def explore_sources(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ExploreSourcePage:
    """Sources whose categories can be browsed.

    The engine has implemented ``explore()`` since M1 but nothing ever called
    it. Sampling the archive: **54.9% of book sources declare explore rules**,
    and 19.3% of the sample both declare them and run without JS -- roughly
    2,500 sources across the full pool.

    This is a table query only (``has_explore`` is computed during scan);
    category names need the source file, so they are filled in for the current
    page alone. Needs ``POST /reader/scan`` to have run.
    """
    page = get_reader_service().explore_sources(limit=limit, offset=offset, q=q)
    return ExploreSourcePage(
        items=[ExploreSourceOut(**item) for item in page["items"]],
        total=page["total"],
        limit=page["limit"],
        offset=page["offset"],
    )


@router.get("/explore/kinds", response_model=list[ExploreKindOut])
def explore_kinds(url_id: int = Query(ge=1)) -> list[ExploreKindOut]:
    """One source's browse categories. Touches no network."""
    with engine_errors():
        return [ExploreKindOut(**item) for item in get_reader_service().explore_kinds(url_id)]


@router.get("/explore", response_model=ExplorePage)
def explore(
    url_id: int = Query(ge=1),
    url: str = Query(min_length=1, max_length=2048),
    page: int = Query(default=1, ge=1, le=500),
) -> ExplorePage:
    """Books in one category of one source.

    Shaped like a search result (each book carries ``sources``) so the detail
    page does not need a second code path -- here ``sources`` always has
    exactly one entry, because browsing is a single-source action.

    ``url`` is the opaque token from ``/explore/kinds``, passed back as-is.
    """
    with engine_errors():
        items = get_reader_service().explore(url_id, url, page=page)
    return ExplorePage(
        items=[SearchBookOut(**item) for item in items], total=len(items), page=page
    )


@router.post("/scan", response_model=ScanReport, status_code=status.HTTP_200_OK)
def scan(
    source_type: str = Query(default="book", pattern="^(book|rss)$"),
    limit: int | None = Query(default=None, ge=1),
) -> ScanReport:
    """Rebuild the candidate pool from the local source archive.

    Required before the first search or subscription-directory browse -- until
    this runs, ``reader_source_prefs`` is empty and both return nothing. The
    two source types have separate pools, so ``source_type=rss`` is a separate
    run, not a side effect of the book one.

    Synchronous and slow (tens of thousands of small files); ``limit`` exists so
    a smoke run stays quick.

    Safe to re-run: it only refreshes the static verdicts and leaves the
    live-result columns (``fail_count``/``last_ok_at``) alone.
    """
    registry = (
        get_rss_service().registry if source_type == "rss" else get_reader_service().registry
    )
    return ScanReport(source_type=source_type, **registry.scan(limit=limit))
