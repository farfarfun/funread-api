"""候选源池的查看与人工干预。

`reader_source_prefs` 一直记着每个源的静态判定与实跑结果 —— 书源池实测 5,606 个
可用源、订阅源 142 个 —— 但在这组端点之前它**完全没有出口**：看不到池子里有什么、
不知道某个源为什么失败、也没法手动停用一个返回垃圾的源或重新启用一个被自动停用的。

这组端点挂在管理端（`require_session`）而不是读者端：它是运维动作，会影响**所有
用户**的搜索结果，不该由任意读者改。和 `/sources` 的区别是那一组管的是**采集源**
（产出书源列表的那些 URL），这一组管的是采集下来的**单个源**。
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter, deque
from typing import Any
from urllib.parse import urlsplit

from farlog import getLogger
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from funread.legado.engine import load_source
from funread.legado.manage.source.storage import SourceDetailRecord, compute_url_md5
from funread.legado.manage.utils import url_to_hostname
from funread.legado.reader import list_source_prefs, record_source_test, reset_source_tests
from funread.legado.reader.registry import _rss_identity
from funread.legado.reader.storage import ReaderSourcePref, get_session_factory
from funread_api.security import CurrentUser, require_reader_admin

from .deps import get_reader_service, get_rss_service, get_source_test_tracker
from .rss_optimize import optimize_rss_rules

logger = getLogger("funread-api")

router = APIRouter(prefix="/pool", tags=["pool"])

SourceType = str


class PoolSourceOut(BaseModel):
    source_type: str
    url_id: int
    name: str
    #: 进不进候选池。扫描按「规则完整 + 不需要 JS + 不是 WebView 型」自动算，
    #: 之后可以被实跑失败自动关掉，也可以在这里人工改。
    enabled: bool
    weight: int
    #: 静态判定：核心链路字段齐不齐。
    is_complete: bool
    #: 静态判定：规则里有没有 JS。
    needs_js: bool
    #: 能不能按分类浏览（发现页）。
    has_explore: bool
    #: 连续失败次数。实跑成功会清零。
    fail_count: int
    #: 主动测试的累计次数与成功次数，以及二者之比。**成功率是候选池的第一排序键**。
    #: 没测过时 `test_count` 为 0，`success_rate` 给 `null`（而不是 0）—— 「没测过」
    #: 和「测了全失败」在界面上必须分得开。
    test_count: int
    test_ok_count: int
    success_rate: float | None
    last_test_at: str
    last_test_ok: bool | None
    #: 最近一次实跑成功的时间。**这是唯一可信的可用性信号** —— 采集侧的
    #: `status==2` 只代表某次 GET 过站点首页，实测预测力极差。
    last_ok_at: str
    last_error: str
    source_url: str = ""
    domain: str = ""


class PoolPage(BaseModel):
    items: list[PoolSourceOut]
    total: int
    limit: int
    offset: int
    #: 整个分区的汇总，和当前这一页无关 —— 界面要能一眼看到池子的健康度。
    summary: dict[str, int]


class PoolPatchIn(BaseModel):
    enabled: bool | None = None
    #: 排序加分。候选池的顺序是「实跑成功过的优先 → 失败少的优先 → 权重高的优先」，
    #: 所以调它可以把某个源顶到前面去。
    weight: int | None = Field(default=None, ge=-100, le=100)
    #: 置真会把 `fail_count` 清零并清掉 `last_error` —— 给「站点恢复了，
    #: 让这个源重新参与」用。
    reset_failures: bool = False


class PoolBatchPatchIn(BaseModel):
    url_ids: list[int] = Field(min_length=1, max_length=20000)
    enabled: bool


class PoolRulesOut(BaseModel):
    source: PoolSourceOut
    rules: dict[str, Any]
    index_key: str
    updated_at: str


class PoolRulesIn(BaseModel):
    rules: dict[str, Any]
    updated_at: str


class PoolOptimizeOut(BaseModel):
    status: str
    reason: str
    before_count: int
    after_count: int
    rules: dict[str, Any]


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _domain(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _to_out(row: ReaderSourcePref, source_url: str = "") -> PoolSourceOut:
    return PoolSourceOut(
        source_type=row.source_type,
        url_id=int(row.url_id),
        name=row.name or "",
        enabled=bool(row.enabled),
        weight=int(row.weight or 0),
        is_complete=bool(row.is_complete),
        needs_js=bool(row.needs_js),
        has_explore=bool(row.has_explore),
        fail_count=int(row.fail_count or 0),
        test_count=int(row.test_count or 0),
        test_ok_count=int(row.test_ok_count or 0),
        success_rate=(
            round(int(row.test_ok_count or 0) / int(row.test_count), 4)
            if int(row.test_count or 0) > 0
            else None
        ),
        last_test_at=_iso(row.last_test_at),
        last_test_ok=row.last_test_ok,
        last_ok_at=_iso(row.last_ok_at),
        last_error=row.last_error or "",
        source_url=source_url,
        domain=_domain(source_url),
    )


def _source_urls(source_type: str, ids: list[int], database_url: str | None) -> dict[int, str]:
    if not ids:
        return {}
    with get_session_factory(database_url)() as session:
        key = "bookSourceUrl" if source_type == "book" else "sourceUrl"
        return {
            url_id: url or ""
            for url_id, url in session.execute(
                select(
                    ReaderSourcePref.url_id, ReaderSourcePref.source_json[key].as_string()
                ).where(
                    ReaderSourcePref.source_type == source_type,
                    ReaderSourcePref.url_id.in_(ids),
                )
            ).all()
        }


def _database_url(source_type: str) -> str | None:
    service = get_rss_service() if source_type == "rss" else get_reader_service()
    return service.database_url


@router.get("", response_model=PoolPage)
def list_pool(
    source_type: str = Query(default="book", pattern="^(book|rss)$"),
    q: str = Query(default="", max_length=64),
    state: str = Query(default="any", pattern="^(any|enabled|disabled|failing|untested)$"),
    sort: str = Query(
        default="rank",
        pattern="^(rank|(url_id|name|domain|source_url|verdict|proven|success_rate|last_test|last_error|weight|enabled)_(asc|desc))$",
    ),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> PoolPage:
    """列出候选源池。

    取全量再在内存里筛，不下推到 SQL：池子最大 13k 行、单表单次查询，而 `state`
    的三个取值要组合 `enabled` 与 `fail_count` 两个条件，下推进去会把
    `list_source_prefs` 的签名撑成一堆布尔开关。要是哪天池子涨到十万行再说。
    """
    rows = list_source_prefs(
        source_type=source_type,
        enabled_only=False,
        database_url=_database_url(source_type),
    )
    summary = {
        "total": len(rows),
        "enabled": sum(1 for row in rows if row.enabled),
        "complete": sum(1 for row in rows if row.is_complete),
        "needs_js": sum(1 for row in rows if row.needs_js),
        "has_explore": sum(1 for row in rows if row.has_explore),
        #  实跑成功过的 —— 这个数字才是池子的真实健康度
        "proven": sum(1 for row in rows if row.last_ok_at is not None),
        "failing": sum(1 for row in rows if (row.fail_count or 0) > 0),
        "last_test_failed_enabled": sum(
            1 for row in rows if row.last_test_ok is False and row.enabled
        ),
        "last_test_success_disabled": sum(
            1 for row in rows if row.last_test_ok is True and not row.enabled
        ),
    }

    keyword = q.strip()
    if keyword:
        rows = [row for row in rows if keyword in (row.name or "")]
    if state == "enabled":
        rows = [row for row in rows if row.enabled]
    elif state == "disabled":
        rows = [row for row in rows if not row.enabled]
    elif state == "failing":
        rows = [row for row in rows if (row.fail_count or 0) > 0]
    elif state == "untested":
        rows = [row for row in rows if int(row.test_count or 0) == 0]

    database_url = _database_url(source_type)
    urls = (
        _source_urls(source_type, [int(row.url_id) for row in rows], database_url)
        if sort.startswith(("domain_", "source_url_"))
        else {}
    )
    if sort != "rank":
        field, direction = sort.rsplit("_", 1)
        keys = {
            "url_id": lambda row: int(row.url_id),
            "name": lambda row: (row.name or "").casefold(),
            "domain": lambda row: _domain(urls.get(int(row.url_id), "")).casefold(),
            "source_url": lambda row: urls.get(int(row.url_id), "").casefold(),
            "verdict": lambda row: bool(row.is_complete),
            "proven": lambda row: row.last_ok_at,
            "success_rate": lambda row: (
                int(row.test_ok_count or 0) / int(row.test_count) if row.test_count else None
            ),
            "last_test": lambda row: row.last_test_at,
            "last_error": lambda row: (row.last_error or "").casefold(),
            "weight": lambda row: int(row.weight or 0),
            "enabled": lambda row: bool(row.enabled),
        }

        def sort_value(row: ReaderSourcePref):
            return keys[field](row)

        present = [row for row in rows if sort_value(row) not in (None, "")]
        missing = [row for row in rows if sort_value(row) in (None, "")]
        present.sort(
            key=lambda row: (sort_value(row), int(row.url_id)), reverse=direction == "desc"
        )
        rows = present + missing

    visible = rows[offset : offset + limit]
    if not urls:
        urls = _source_urls(source_type, [int(row.url_id) for row in visible], database_url)

    return PoolPage(
        items=[_to_out(row, urls.get(int(row.url_id), "")) for row in visible],
        total=len(rows),
        limit=limit,
        offset=offset,
        summary=summary,
    )


@router.patch("/batch/{source_type}")
def patch_pool_sources(source_type: str, payload: PoolBatchPatchIn) -> dict[str, int]:
    if source_type not in ("book", "rss"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="source_type 只能是 book 或 rss",
        )
    ids = set(payload.url_ids)
    session_factory = get_session_factory(_database_url(source_type))
    with session_factory() as session:
        result = session.execute(
            update(ReaderSourcePref)
            .where(ReaderSourcePref.source_type == source_type, ReaderSourcePref.url_id.in_(ids))
            .values(enabled=payload.enabled, enabled_override=payload.enabled)
        )
        session.commit()
        return {"updated": result.rowcount}


@router.patch("/batch/{source_type}/latest-test/{outcome}")
def patch_pool_by_latest_test(source_type: str, outcome: str) -> dict[str, int]:
    if source_type not in ("book", "rss") or outcome not in ("success", "failure"):
        raise HTTPException(status_code=422, detail="无效的源类型或测试结果")
    enable = outcome == "success"
    with get_session_factory(_database_url(source_type))() as session:
        result = session.execute(
            update(ReaderSourcePref)
            .where(
                ReaderSourcePref.source_type == source_type,
                ReaderSourcePref.last_test_ok.is_(enable),
                ReaderSourcePref.enabled.is_(not enable),
            )
            .values(enabled=enable, enabled_override=enable)
        )
        session.commit()
        return {"updated": int(result.rowcount or 0)}


@router.patch("/{source_type}/{url_id}", response_model=PoolSourceOut)
def patch_pool_source(
    source_type: str,
    url_id: int,
    payload: PoolPatchIn,
) -> PoolSourceOut:
    """人工干预一个源：启停、调权重、清失败计数。

    会影响所有用户的搜索结果，所以这是管理端动作。`reset_failures` 的用途是
    「站点恢复了」—— 结构性不支持（需要 JS）的源被 `disable_after=1` 关掉后，
    清计数并重新启用是没有意义的，但站点临时抽风导致的失败值得给第二次机会。
    """
    if source_type not in ("book", "rss"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="source_type 只能是 book 或 rss",
        )

    session_factory = get_session_factory(_database_url(source_type))
    with session_factory() as session:
        row = session.get(ReaderSourcePref, (source_type, int(url_id)))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="池子里没有这个源")
        if payload.enabled is not None:
            row.enabled = payload.enabled
            row.enabled_override = payload.enabled
        if payload.weight is not None:
            row.weight = payload.weight
        if payload.reset_failures:
            row.fail_count = 0
            row.last_error = None
        session.commit()
        session.refresh(row)
        urls = _source_urls(source_type, [int(url_id)], _database_url(source_type))
        return _to_out(row, urls.get(int(url_id), ""))


def _source_rules_out(row: ReaderSourcePref, index_key: str) -> PoolRulesOut:
    key = "bookSourceUrl" if row.source_type == "book" else "sourceUrl"
    rules = row.source_json or {}
    return PoolRulesOut(
        source=_to_out(row, str(rules.get(key) or "")),
        rules=rules,
        index_key=index_key,
        updated_at=_iso(row.updated_at),
    )


@router.get("/{source_type}/{url_id}/rules", response_model=PoolRulesOut)
def get_pool_rules(source_type: str, url_id: int) -> PoolRulesOut:
    if source_type not in ("book", "rss"):
        raise HTTPException(status_code=422, detail="source_type 只能是 book 或 rss")
    with get_session_factory(_database_url(source_type))() as session:
        row = session.get(ReaderSourcePref, (source_type, url_id))
        if row is None or row.source_json is None:
            raise HTTPException(status_code=404, detail="没有这个源的规则")
        index = session.scalar(
            select(SourceDetailRecord.url).where(
                SourceDetailRecord.source_type == source_type, SourceDetailRecord.id == url_id
            )
        )
        return _source_rules_out(row, index or "")


@router.put("/{source_type}/{url_id}/rules", response_model=PoolRulesOut)
def save_pool_rules(source_type: str, url_id: int, payload: PoolRulesIn) -> PoolRulesOut:
    if source_type not in ("book", "rss"):
        raise HTTPException(status_code=422, detail="source_type 只能是 book 或 rss")
    key = "bookSourceUrl" if source_type == "book" else "sourceUrl"
    if len(json.dumps(payload.rules, ensure_ascii=False).encode("utf-8")) > 256_000:
        raise HTTPException(status_code=422, detail="规则内容超过 256 KB")
    database_url = _database_url(source_type)
    with get_session_factory(database_url)() as session:
        row = session.scalar(
            select(ReaderSourcePref)
            .where(ReaderSourcePref.source_type == source_type, ReaderSourcePref.url_id == url_id)
            .with_for_update()
        )
        if row is None or row.source_json is None:
            raise HTTPException(status_code=404, detail="没有这个源的规则")
        if _iso(row.updated_at) != payload.updated_at:
            raise HTTPException(status_code=409, detail="规则已被修改，请重新打开后再编辑")
        old_url = row.source_json.get(key)
        new_url = payload.rules.get(key)
        if not isinstance(new_url, str) or not new_url.strip():
            raise HTTPException(status_code=422, detail=f"{key} 不能为空")
        try:
            spec = load_source(payload.rules, source_type=source_type)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"规则格式无效：{exc}") from exc
        index = session.scalar(
            select(SourceDetailRecord).where(
                SourceDetailRecord.source_type == source_type, SourceDetailRecord.id == url_id
            )
        )
        if new_url != old_url:
            try:
                parsed = urlsplit(new_url)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="源地址格式无效") from exc
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise HTTPException(status_code=422, detail="源地址需要完整的 http(s) URL")
            new_index = (
                url_to_hostname(new_url) if source_type == "book" else _rss_identity(new_url)
            )
            if index is None:
                raise HTTPException(status_code=409, detail="此源缺少地址映射，无法修改源地址")
            duplicate = session.scalar(
                select(SourceDetailRecord.id).where(
                    SourceDetailRecord.source_type == source_type,
                    SourceDetailRecord.url_md5 == compute_url_md5(new_index),
                    SourceDetailRecord.id != url_id,
                )
            )
            if duplicate is not None:
                raise HTTPException(status_code=409, detail="这个地址已属于另一个源")
            index.url = new_index
            index.url_md5 = compute_url_md5(new_index)
        row.source_json = payload.rules
        row.name = spec.name[:255]
        row.is_complete = spec.is_complete
        row.needs_js = spec.needs_js()
        row.has_explore = bool(
            any(kind.url for kind in spec.explore_kinds())
            and (
                spec.has_rule("ruleExplore", "bookList") or spec.has_rule("ruleSearch", "bookList")
            )
        )
        if not row.is_complete or spec.is_web_view:
            row.enabled = False
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="源地址已存在") from exc
        session.refresh(row)
        result = _source_rules_out(row, index.url if index else "")
    service = get_reader_service() if source_type == "book" else get_rss_service()
    service.registry.invalidate(url_id)
    return result


@router.post("/rss/{url_id}/optimize", response_model=PoolOptimizeOut)
def optimize_pool_rss(url_id: int) -> PoolOptimizeOut:
    with get_session_factory(_database_url("rss"))() as session:
        row = session.get(ReaderSourcePref, ("rss", url_id))
        if row is None or row.source_json is None:
            raise HTTPException(status_code=404, detail="没有这个订阅源的规则")
        rules = row.source_json
    return PoolOptimizeOut(**optimize_rss_rules(rules))


# ------------------------------------------------------------------ 批量测试
#
# 「这个源现在还能用吗」只有真跑一次才知道。采集侧的 `status==2` 预测力极差
# （实测 100 个标可用的源只有 21 个能跑通），而用户搜索时顺带记下的
# `fail_count`/`last_ok_at` 又太稀疏：没被选中的源永远没有记录，没记录又让它
# 更难被选中，自我固化。
#
# 批量测试打破这个循环：每个源都测、可以反复测，累计成功率成为候选池的第一
# 排序键。跑一轮给每个源记一次，跑 10 轮就是 n/10。
#
# 两种源的测法不同但判据一致（解析出东西才算成功）：订阅源抓一次文章列表，
# 书源拿固定关键词「我的」搜一次。用户真实使用的结果也进同一组计数，所以
# 样本会随着用得越多越准。


class SourceTestIn(BaseModel):
    source_type: str = Field(default="rss", pattern="^(book|rss)$")
    #: 一轮最多测几个。默认 50：串行执行，一个源最坏要等满超时，再多就把一次
    #: 「点一下看看」拖成十几分钟。书源池有 5,606 个，想测全得分很多轮。
    limit: int = Field(default=50, ge=1, le=500)
    #: 只测没测过的。想重新评估全部时置 false。
    untested_only: bool = False
    #: 显式指定源时可测试已停用的源；不传则沿用候选池批量测试。
    url_ids: list[int] | None = Field(default=None, min_length=1, max_length=500)


class SourceTestAccepted(BaseModel):
    task_id: str
    #: 实际排进这一轮的源数量。
    queued: int


class SourceTestProgress(BaseModel):
    task_id: str
    state: str
    total: int
    done: int
    failed: int
    detail: str


class SourceTestQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: deque[tuple[str, str, list[int], str | None]] = deque()
        self._pending: Counter[tuple[str, int]] = Counter()
        self._active: str | None = None
        self._cancelled: set[str] = set()
        self._last_domain_test: dict[str, float] = {}
        self._worker: threading.Thread | None = None

    def submit(
        self,
        source_type: str,
        candidates: list[int],
        limit: int,
        explicit: bool,
        user_id: int,
        database_url: str | None,
    ) -> SourceTestAccepted | None:
        with self._lock:
            targets = (
                candidates
                if explicit
                else [url_id for url_id in candidates if not self._pending[source_type, url_id]][
                    :limit
                ]
            )
            if not targets:
                return None
            task = get_source_test_tracker().start("source-test", user_id, total=len(targets))
            for url_id in targets:
                self._pending[source_type, url_id] += 1
            self._jobs.append((task.task_id, source_type, targets, database_url))
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._consume, name="source-tests", daemon=True
                )
                self._worker.start()
            return SourceTestAccepted(task_id=task.task_id, queued=len(targets))

    def _consume(self) -> None:
        while True:
            with self._lock:
                if not self._jobs:
                    self._worker = None
                    self._last_domain_test.clear()
                    return
                task_id, source_type, targets, database_url = self._jobs.popleft()
                self._active = task_id
            try:
                _run_source_test(task_id, source_type, targets, database_url)
            except Exception:  # noqa: BLE001 — 单个任务出错不能停掉后续任务
                logger.exception("候选源测试任务异常")
                get_source_test_tracker().fail(task_id, "测试任务异常中断")
            finally:
                with self._lock:
                    self._active = None
                    self._cancelled.discard(task_id)
                    for url_id in targets:
                        key = source_type, url_id
                        self._pending[key] -= 1
                        if not self._pending[key]:
                            del self._pending[key]

    def cancelled(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancelled

    def wait_for_domain(self, task_id: str, domain: str) -> bool:
        # Each domain starts at most one test every 30 seconds across queued batches.
        domain = domain or "unknown"
        while True:
            with self._lock:
                if task_id in self._cancelled:
                    return False
                now = time.monotonic()
                delay = self._last_domain_test.get(domain, 0) + 30 - now
                if delay <= 0:
                    self._last_domain_test[domain] = now
                    return True
            time.sleep(min(delay, 1))

    def clear(self, user_id: int) -> int:
        tracker = get_source_test_tracker()
        with self._lock:
            kept = deque()
            cancelled = 0
            for job in self._jobs:
                task_id, source_type, targets, _ = job
                task = tracker.get(task_id)
                if task is None or task.user_id != user_id:
                    kept.append(job)
                    continue
                tracker.cancel(task_id)
                cancelled += 1
                for url_id in targets:
                    key = source_type, url_id
                    self._pending[key] -= 1
                    if not self._pending[key]:
                        del self._pending[key]
            self._jobs = kept
            if self._active:
                task = tracker.get(self._active)
                if task is not None and task.user_id == user_id:
                    self._cancelled.add(self._active)
                    tracker.cancel(self._active)
                    cancelled += 1
            return cancelled


# shortcut: 队列和进度仅在单个 API 进程内可见；多实例负载均衡前需迁到共享任务存储。
_source_test_queue = SourceTestQueue()


@router.post("/test", response_model=SourceTestAccepted, status_code=status.HTTP_202_ACCEPTED)
def start_source_test(
    payload: SourceTestIn,
    actor: CurrentUser = Depends(require_reader_admin),
) -> SourceTestAccepted:
    """排一轮批量测试，立刻返回 task_id；单个消费者串行处理所有轮次。"""
    database_url = _database_url(payload.source_type)
    rows = list_source_prefs(
        source_type=payload.source_type,
        enabled_only=payload.url_ids is None,
        limit=None,
        database_url=database_url,
    )
    if payload.url_ids is not None:
        available = {int(row.url_id) for row in rows}
        candidates = list(
            dict.fromkeys(url_id for url_id in payload.url_ids if url_id in available)
        )
    else:
        if payload.untested_only:
            rows = [row for row in rows if int(row.test_count or 0) == 0]
        #  成绩差和没测过的排在后面，先测它们。
        candidates = [int(row.url_id) for row in rows[::-1]]
    accepted = _source_test_queue.submit(
        payload.source_type,
        candidates,
        payload.limit,
        payload.url_ids is not None,
        actor.user_id,
        database_url,
    )
    if accepted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有可测的源")
    return accepted


@router.post("/test/all", response_model=SourceTestAccepted, status_code=status.HTTP_202_ACCEPTED)
def test_all_sources(
    source_type: str = Query(default="book", pattern="^(book|rss)$"),
    actor: CurrentUser = Depends(require_reader_admin),
) -> SourceTestAccepted:
    database_url = _database_url(source_type)
    rows = list_source_prefs(
        source_type=source_type, enabled_only=False, with_rules_only=True, database_url=database_url
    )
    rows.sort(
        key=lambda row: (
            row.last_test_at is not None,
            row.last_test_at or row.created_at,
            int(row.url_id),
        )
    )
    candidates = [int(row.url_id) for row in rows]
    accepted = _source_test_queue.submit(
        source_type, candidates, len(candidates), False, actor.user_id, database_url
    )
    if accepted is None:
        raise HTTPException(status_code=409, detail="没有待测试的有规则源，或全部已在队列中")
    return accepted


@router.delete("/test/queue")
def clear_source_test_queue(actor: CurrentUser = Depends(require_reader_admin)) -> dict[str, int]:
    return {"cancelled": _source_test_queue.clear(actor.user_id)}


def _run_source_test(
    task_id: str, source_type: str, url_ids: list[int], database_url: str | None
) -> None:
    """后台逐个测。**串行**，不并发。

    这些请求直接打在别人的站点上，而源站普遍脆弱（实测大量 403 和连接拒绝）。
    并发测只会让我们更快被封，而这是个后台任务，没人等着它立刻返回。

    两种源的「测试」语义不同，各自的 service 负责定义：订阅源是抓一次文章列表，
    书源是拿固定关键词搜一次。共同点是**判据都为「解析出东西」而不是 HTTP 200**。
    """
    tracker = get_source_test_tracker()
    service = get_reader_service() if source_type == "book" else get_rss_service()
    done = 0
    failed = 0
    try:
        urls = _source_urls(source_type, url_ids, database_url)
        for url_id in url_ids:
            if _source_test_queue.cancelled(task_id):
                return
            if not _source_test_queue.wait_for_domain(task_id, _domain(urls.get(url_id, ""))):
                return
            ok, error = service.test_source(url_id)
            record_source_test(
                source_type=source_type,
                url_id=url_id,
                ok=ok,
                error=error,
                database_url=database_url,
            )
            if ok:
                done += 1
            else:
                failed += 1
            tracker.progress(task_id, done=done + failed, failed=failed)
        tracker.finish(task_id, done=done + failed, failed=failed)
    except Exception as exc:  # noqa: BLE001 — 后台任务，异常只能记在 tracker 上
        logger.exception("批量测试异常中断")
        tracker.fail(task_id, f"{type(exc).__name__}: {exc}")


@router.get("/test/{task_id}", response_model=SourceTestProgress)
def source_test_progress(
    task_id: str, actor: CurrentUser = Depends(require_reader_admin)
) -> SourceTestProgress:
    task = get_source_test_tracker().get(task_id)
    if task is None or task.user_id != actor.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这个任务")
    return SourceTestProgress(
        task_id=task.task_id,
        state=task.state,
        total=task.total,
        done=task.done,
        failed=task.failed,
        detail=task.detail or "",
    )


@router.delete("/test/{source_type}/scores", status_code=status.HTTP_200_OK)
def reset_scores(
    source_type: str,
    url_id: int | None = Query(default=None),
    actor: CurrentUser = Depends(require_reader_admin),
) -> dict[str, int]:
    """清空测试计数。源站换了域名、规则修好了之后，旧样本会一直压着它排不上来。"""
    if source_type not in ("book", "rss"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="source_type 只能是 book 或 rss",
        )
    affected = reset_source_tests(
        source_type=source_type, url_id=url_id, database_url=_database_url(source_type)
    )
    return {"reset": affected}
