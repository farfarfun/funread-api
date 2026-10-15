"""候选源池的查看与人工干预。

`reader_source_prefs` 一直记着每个源的静态判定与实跑结果 —— 书源池实测 5,606 个
可用源、订阅源 142 个 —— 但在这组端点之前它**完全没有出口**：看不到池子里有什么、
不知道某个源为什么失败、也没法手动停用一个返回垃圾的源或重新启用一个被自动停用的。

这组端点挂在管理端（`require_session`）而不是读者端：它是运维动作，会影响**所有
用户**的搜索结果，不该由任意读者改。和 `/sources` 的区别是那一组管的是**采集源**
（产出书源列表的那些 URL），这一组管的是采集下来的**单个源**。
"""

from __future__ import annotations

from farlog import getLogger
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from funread.legado.reader import list_source_prefs, record_source_test, reset_source_tests
from funread.legado.reader.storage import ReaderSourcePref, get_session_factory
from funread_api.security import CurrentUser, require_reader_admin

from .deps import get_reader_service, get_rss_service, get_source_test_tracker

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
        test_count=int(row.test_count or 0),
        test_ok_count=int(row.test_ok_count or 0),
        success_rate=(
            round(int(row.test_ok_count or 0) / int(row.test_count), 4)
            if int(row.test_count or 0) > 0
            else None
        ),
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
        if payload.weight is not None:
            row.weight = payload.weight
        if payload.reset_failures:
            row.fail_count = 0
            row.last_error = None
        session.commit()
        session.refresh(row)
        return _to_out(row)


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


@router.post("/test", response_model=SourceTestAccepted, status_code=status.HTTP_202_ACCEPTED)
def start_source_test(
    payload: SourceTestIn,
    background: BackgroundTasks,
    actor: CurrentUser = Depends(require_reader_admin),
) -> SourceTestAccepted:
    """排一轮批量测试，立刻返回 task_id，进度去 `GET /pool/test/{task_id}` 取。

    同一个人同时只跑一轮：两轮并行除了把目标站点的请求翻倍没有任何好处，而这些
    站点本来就脆弱。
    """
    tracker = get_source_test_tracker()
    running = tracker.active_for("source-test", actor.user_id)
    if running is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"已经有一轮测试在跑了（{running.done}/{running.total}）",
        )

    database_url = _database_url(payload.source_type)
    rows = list_source_prefs(
        source_type=payload.source_type,
        enabled_only=True,
        limit=None,
        database_url=database_url,
    )
    if payload.untested_only:
        rows = [row for row in rows if int(row.test_count or 0) == 0]
    #  倒着取：`list_source_prefs` 已经按成功率排好序，没测过和成绩差的在后面，
    #  而它们正是最需要重新测的。排在前面的那些刚证明过自己。
    targets = [int(row.url_id) for row in rows[::-1][: payload.limit]]
    if not targets:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有可测的源")

    task = tracker.start("source-test", actor.user_id, total=len(targets))
    background.add_task(_run_source_test, task.task_id, payload.source_type, targets, database_url)
    return SourceTestAccepted(task_id=task.task_id, queued=len(targets))


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
        for url_id in url_ids:
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
