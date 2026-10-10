"""Shelf, groups, update checks, reading progress, and the offline chapter cache."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from funread.legado.reader import (
    clear_chapter_cache,
    get_shelf_book,
    list_cached_chapter_indexes,
)
from funread_api.security import CurrentUser, require_user

from .deps import (
    TaskTracker,
    get_download_tracker,
    get_reader_service,
    get_update_check_tracker,
)
from .reader import ChapterModel

router = APIRouter(prefix="/shelf", tags=["shelf"])

#: Cap on one download request. The service fetches serially with a pause
#: between chapters, so a 3,000-chapter book would hold a worker for an hour.
#: The client walks the toc in slices instead.
MAX_DOWNLOAD_CHAPTERS = 200

#: Cap on one update-check round. Same reason as the download cap -- the check
#: is serial with a pause between books -- but a shelf is orders of magnitude
#: smaller than a toc, so this is high enough that it never bites in practice.
MAX_CHECK_BOOKS = 500

#: 分组名长度上限。分组是前端的标签，不是实体，没必要给长名字留空间。
MAX_GROUP_LENGTH = 128


class ShelfBookIn(BaseModel):
    name: str
    author: str = ""
    cover_url: str = ""
    intro: str = ""
    url_id: int | None = None
    book_url: str = ""
    toc_url: str = ""
    last_chapter: str = ""
    #: 分组。`None`（默认）= 不动现有分组 —— 这个端点是幂等的，从搜索结果里再点
    #: 一次「加入书架」不该把书从用户分好的组里踢回未分组。空串才是显式「未分组」。
    group: str | None = Field(default=None, max_length=MAX_GROUP_LENGTH)


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
    group: str = ""
    #: 上次检查更新时数到的章节数。0 = 还没查过，不是「没有章节」。
    chapter_count: int = 0
    #: 未读章节数，由 `chapter_count` 和进度**算出来**的，不落库 —— 存一份就得在
    #: 每次保存进度时同步维护，而它随时能算。
    unread: int = 0
    #: 空串 = 还没查过。沿用 `updated_at` 的约定，不用 `null`。
    last_checked_at: str = ""
    #: 上次检查失败的原因。成功一次就清掉。
    last_check_error: str = ""


class BookKeyOut(BaseModel):
    book_key: str


class ShelfGroupOut(BaseModel):
    name: str
    count: int


class AssignGroupIn(BaseModel):
    #: 要移动的书。空列表直接 400 —— 让它静默返回 0 只会让前端的 bug 更难发现。
    book_keys: list[str] = Field(min_length=1, max_length=MAX_CHECK_BOOKS)
    #: 目标分组。空串 = 移出分组。
    group: str = Field(default="", max_length=MAX_GROUP_LENGTH)


class RenameGroupIn(BaseModel):
    #: 原分组名。不接受空串 —— 「给所有未分组的书起个名」是批量移动（用
    #: `/groups/assign`），不是重命名，混在一起会让一次误操作扫掉整个书架。
    old: str = Field(min_length=1, max_length=MAX_GROUP_LENGTH)
    #: 新分组名。空串 = 解散这个分组，书退回未分组。
    new: str = Field(default="", max_length=MAX_GROUP_LENGTH)


class AffectedOut(BaseModel):
    #: 动了几本书。前端用它说「已移动 N 本」。
    affected: int


class CheckUpdatesIn(BaseModel):
    #: 要检查哪几本。`None` = 整个书架。
    book_keys: list[str] | None = Field(default=None, max_length=MAX_CHECK_BOOKS)
    #: 每本之间停多久。暴露出来是给测试和「我就查一本」用的。
    interval: float = Field(default=1.0, ge=0, le=5)


class CheckUpdatesAccepted(BaseModel):
    task_id: str
    queued: int


class CheckProgress(BaseModel):
    task_id: str
    state: str
    total: int
    #: 已经查完的本数（成功的）。
    done: int
    failed: int
    detail: str = ""


class SwitchSourceIn(BaseModel):
    url_id: int
    book_url: str
    #: 要不要把阅读进度重新定位到新源的目录里。关掉可以省一次目录抓取，
    #: 适用于「还没开始读就换源」。
    remap_progress: bool = True


class SwitchSourceOut(BaseModel):
    """换源后进度落在哪一章。

    `method` 决定界面该怎么说：
      - `exact` / `normalized` —— 按章节名定位到了，可以直接接着读；
      - `position` —— 按比例估的，**必然不准**，界面要提示用户确认；
      - `none` —— 新源的目录取不到，进度没动，要用户手动选章；
      - `skipped` —— 本来就没有进度可搬。
    """

    method: str
    chapter_index: int
    chapter_name: str
    total: int
    is_approximate: bool = False


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
def list_books(
    group: str | None = Query(
        default=None,
        max_length=MAX_GROUP_LENGTH,
        description="分组过滤。不传 = 整个书架，传空串 = 只看未分组的书。",
    ),
    user: CurrentUser = Depends(require_user),
) -> list[ShelfBookOut]:
    return [
        ShelfBookOut(**item)
        for item in get_reader_service().shelf(user_id=user.user_id, group=group)
    ]


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


#  分组与检查更新的路由必须排在 `/{book_key}` 之前 —— 否则 `/shelf/groups` 会被
#  当成 book_key="groups" 匹配掉。
@router.get("/groups", response_model=list[ShelfGroupOut])
def list_groups(user: CurrentUser = Depends(require_user)) -> list[ShelfGroupOut]:
    """这个人书架上的分组，带本数。未分组那一组名字是空串，排在最后。"""
    return [ShelfGroupOut(**item) for item in get_reader_service().shelf_groups(user.user_id)]


@router.post("/groups/assign", response_model=AffectedOut)
def assign_group(
    payload: AssignGroupIn,
    user: CurrentUser = Depends(require_user),
) -> AffectedOut:
    """把几本书移进一个分组（或移出，`group=""`）。

    不校验这些 book_key 在不在架上：底层 UPDATE 的 WHERE 里带了 `user_id`，不在
    架上的 key 只是匹配不到行。返回实际动了几本，前端据此判断是否有过期数据。
    """
    affected = get_reader_service().set_shelf_group(
        payload.book_keys, payload.group, user_id=user.user_id
    )
    return AffectedOut(affected=affected)


@router.post("/groups/rename", response_model=AffectedOut)
def rename_group(
    payload: RenameGroupIn,
    user: CurrentUser = Depends(require_user),
) -> AffectedOut:
    """改名，或 `new=""` 解散分组。分组没有自己的表，所以这就是一条 UPDATE。"""
    affected = get_reader_service().rename_shelf_group(
        payload.old, payload.new, user_id=user.user_id
    )
    if not affected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这个分组")
    return AffectedOut(affected=affected)


@router.post(
    "/check-updates",
    response_model=CheckUpdatesAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def check_updates(
    payload: CheckUpdatesIn,
    tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_user),
) -> CheckUpdatesAccepted:
    """重新数一遍每本书的章节数，算出未读角标。

    202 而不是直接给结果：一本书要两个请求，书架上几十本就是几分钟，撑不过任何
    请求超时。进度查 ``GET /shelf/check-updates/{task_id}``，结果本身落在书架的
    `chapter_count` / `unread` 上 —— 任务条目掉了也不丢数据。

    一个人同一时刻只允许一轮（``scope=""``）：两轮并行只会把同一批源打两遍。
    """
    service = get_reader_service()
    total = len(service.shelf(user_id=user.user_id, group=None))
    if payload.book_keys is not None:
        total = len(set(payload.book_keys))
    if not total:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="没有要检查的书"
        )

    tracker = get_update_check_tracker()
    running = tracker.active_for("", user.user_id)
    if running is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"正在检查更新（task_id={running.task_id}）",
        )

    task = tracker.start("", user.user_id, total)

    def _run() -> None:
        def _on_progress(stats: dict[str, int]) -> None:
            tracker.progress(
                task.task_id, done=stats.get("checked", 0), failed=stats.get("failed", 0)
            )

        try:
            stats = service.check_updates(
                user_id=user.user_id,
                book_keys=payload.book_keys,
                interval=payload.interval,
                on_progress=_on_progress,
            )
        except Exception as error:
            #  和下载那边同一个理由：任务跑在响应之后，异常只会进服务端日志，
            #  界面会一直转圈。
            tracker.fail(task.task_id, str(error))
            return
        tracker.finish(
            task.task_id, done=int(stats.get("checked", 0)), failed=int(stats.get("failed", 0))
        )

    tasks.add_task(_run)
    return CheckUpdatesAccepted(task_id=task.task_id, queued=total)


@router.get("/check-updates/{task_id}", response_model=CheckProgress)
def check_updates_progress(
    task_id: str,
    user: CurrentUser = Depends(require_user),
) -> CheckProgress:
    """一轮检查进行到哪了。条目在结束十分钟后清掉，之后书架本身就是答案。"""
    task = get_update_check_tracker().get(task_id)
    if task is None or task.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这个检查任务")
    return CheckProgress(
        task_id=task.task_id,
        state=task.state,
        total=task.total,
        done=task.done,
        failed=task.failed,
        detail=task.detail,
    )


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


@router.post("/{book_key}/source", response_model=SwitchSourceOut)
def switch_source(
    book_key: str,
    payload: SwitchSourceIn,
    user: CurrentUser = Depends(require_user),
) -> SwitchSourceOut:
    """Point the book at another source, and re-locate the reading progress.

    Drops the chapter cache on purpose: chapter numbering differs between
    sites, so keeping it would mix chapters.

    **Re-locating the progress is a correctness matter, not a nicety.** Without
    it the shelf still says "read up to chapter 500" while that index now
    points at different content in the new source -- "continue reading" would
    land somewhere unrelated with no way to tell something went wrong. The
    response says *how* it was located so the UI can flag an approximate hit
    instead of pretending it nailed it.

    Returns 200 with that verdict rather than 204: the caller needs the index to
    navigate to, and needs to know whether to trust it.
    """
    _require_shelf_book(book_key, user)
    #  Not inside engine_errors(): the switch itself is a local DB write that
    #  has already succeeded by the time the remap runs. A failed TOC fetch
    #  comes back as method="none", not as a 502 that would wrongly suggest the
    #  source was not switched at all.
    return SwitchSourceOut(
        **get_reader_service().switch_source(
            book_key,
            url_id=payload.url_id,
            book_url=payload.book_url,
            user_id=user.user_id,
            remap_progress=payload.remap_progress,
        )
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
        tracker.finish(
            task.task_id,
            #  已缓存的章节也算「完成」—— 用户要看的是「这批还剩多少」，而不是
            #  「这批里有几章是刚抓的」。
            done=int(stats.get("downloaded", 0)) + int(stats.get("cached", 0)),
            failed=int(stats.get("failed", 0)),
        )

    tasks.add_task(_run)
    return DownloadAccepted(book_key=book_key, queued=len(chapters), task_id=task.task_id)


def _progress_of(tracker: TaskTracker, task_id: str) -> DownloadProgress | None:
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
    if task is None or task.scope != book_key or task.user_id != user.user_id:
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
