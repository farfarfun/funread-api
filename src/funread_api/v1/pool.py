"""候选源池的查看与人工干预。

`reader_source_prefs` 一直记着每个源的静态判定与实跑结果 —— 书源池实测 5,606 个
可用源、订阅源 142 个 —— 但在这组端点之前它**完全没有出口**：看不到池子里有什么、
不知道某个源为什么失败、也没法手动停用一个返回垃圾的源或重新启用一个被自动停用的。

这组端点挂在管理端（`require_session`）而不是读者端：它是运维动作，会影响**所有
用户**的搜索结果，不该由任意读者改。和 `/sources` 的区别是那一组管的是**采集源**
（产出书源列表的那些 URL），这一组管的是采集下来的**单个源**。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from funread.legado.reader import list_source_prefs
from funread.legado.reader.storage import ReaderSourcePref, get_session_factory

from .deps import get_reader_service, get_rss_service

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
    #: 最近一次实跑成功的时间。**这是唯一可信的可用性信号** —— 采集侧的
    #: `status==2` 只代表某次 GET 过站点首页，实测预测力极差。
    last_ok_at: str
    last_error: str


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


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _to_out(row: ReaderSourcePref) -> PoolSourceOut:
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
        last_ok_at=_iso(row.last_ok_at),
        last_error=row.last_error or "",
    )


def _database_url(source_type: str) -> str | None:
    service = get_rss_service() if source_type == "rss" else get_reader_service()
    return service.database_url


@router.get("", response_model=PoolPage)
def list_pool(
    source_type: str = Query(default="book", pattern="^(book|rss)$"),
    q: str = Query(default="", max_length=64),
    #: `any` 不过滤；其余三个是界面上最常问的三个问题。
    state: str = Query(default="any", pattern="^(any|enabled|disabled|failing)$"),
    limit: int = Query(default=50, ge=1, le=500),
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

    return PoolPage(
        items=[_to_out(row) for row in rows[offset : offset + limit]],
        total=len(rows),
        limit=limit,
        offset=offset,
        summary=summary,
    )


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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="source_type 只能是 book 或 rss"
        )

    session_factory = get_session_factory(_database_url(source_type))
    with session_factory() as session:
        row = session.get(ReaderSourcePref, (source_type, int(url_id)))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="池子里没有这个源")
        if payload.enabled is not None:
            row.enabled = payload.enabled
        if payload.weight is not None:
            row.weight = payload.weight
        if payload.reset_failures:
            row.fail_count = 0
            row.last_error = None
        session.commit()
        session.refresh(row)
        return _to_out(row)
