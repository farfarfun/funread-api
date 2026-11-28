"""Per-user Flutter client features: bookmarks, read records, replace rules, home layout, explore pins, preferences."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from funread.legado.manage.source.storage import utcnow
from funread_api.security import CurrentUser, require_user
from funread_api.user_data import (
    BookmarkRecord,
    ExplorePinRecord,
    ReadRecordDaily,
    ReplaceRuleRecord,
    UserPreferencesRecord,
    _session_factory,
)

router = APIRouter(tags=["client"])


def _iso(value) -> str:
    return value.isoformat() if value else ""


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------


class BookmarkIn(BaseModel):
    book_key: str = Field(min_length=1, max_length=32)
    book_name: str = Field(default="", max_length=255)
    book_author: str = Field(default="", max_length=255)
    chapter_index: int = Field(default=0, ge=0)
    chapter_name: str = Field(default="", max_length=512)
    char_offset: int = Field(default=0, ge=0)
    book_text: str = Field(default="", max_length=4000)
    content: str = Field(default="", max_length=4000)


class BookmarkUpdateIn(BaseModel):
    book_text: str | None = Field(default=None, max_length=4000)
    content: str | None = Field(default=None, max_length=4000)


class BookmarkOut(BaseModel):
    id: int
    book_key: str
    book_name: str
    book_author: str
    chapter_index: int
    chapter_name: str
    char_offset: int
    book_text: str
    content: str
    created_at: str


def _bookmark_out(row: BookmarkRecord) -> BookmarkOut:
    return BookmarkOut(
        id=int(row.id),
        book_key=row.book_key,
        book_name=row.book_name,
        book_author=row.book_author,
        chapter_index=int(row.chapter_index),
        chapter_name=row.chapter_name,
        char_offset=int(row.char_offset),
        book_text=row.book_text,
        content=row.content,
        created_at=_iso(row.created_at),
    )


@router.get("/bookmarks", response_model=list[BookmarkOut])
def list_bookmarks(
    book_key: str | None = Query(default=None, max_length=32),
    q: str = Query(default="", max_length=128),
    user: CurrentUser = Depends(require_user),
) -> list[BookmarkOut]:
    with _session_factory()() as session:
        stmt = select(BookmarkRecord).where(BookmarkRecord.user_id == int(user.user_id))
        if book_key:
            stmt = stmt.where(BookmarkRecord.book_key == book_key)
        stmt = stmt.order_by(BookmarkRecord.created_at.desc(), BookmarkRecord.id.desc())
        rows = list(session.execute(stmt).scalars().all())
    if q.strip():
        needle = q.strip().lower()
        rows = [
            row
            for row in rows
            if needle in row.book_name.lower()
            or needle in row.chapter_name.lower()
            or needle in row.book_text.lower()
            or needle in row.content.lower()
        ]
    return [_bookmark_out(row) for row in rows]


@router.post("/bookmarks", response_model=BookmarkOut, status_code=status.HTTP_201_CREATED)
def create_bookmark(
    payload: BookmarkIn,
    user: CurrentUser = Depends(require_user),
) -> BookmarkOut:
    with _session_factory()() as session:
        row = BookmarkRecord(
            user_id=int(user.user_id),
            book_key=payload.book_key,
            book_name=payload.book_name,
            book_author=payload.book_author,
            chapter_index=payload.chapter_index,
            chapter_name=payload.chapter_name,
            char_offset=payload.char_offset,
            book_text=payload.book_text,
            content=payload.content,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _bookmark_out(row)


@router.put("/bookmarks/{bookmark_id}", response_model=BookmarkOut)
def update_bookmark(
    bookmark_id: int,
    payload: BookmarkUpdateIn,
    user: CurrentUser = Depends(require_user),
) -> BookmarkOut:
    with _session_factory()() as session:
        row = session.get(BookmarkRecord, bookmark_id)
        if row is None or int(row.user_id) != int(user.user_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="书签不存在")
        if payload.book_text is not None:
            row.book_text = payload.book_text
        if payload.content is not None:
            row.content = payload.content
        session.commit()
        session.refresh(row)
        return _bookmark_out(row)


@router.delete(
    "/bookmarks/{bookmark_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_bookmark(
    bookmark_id: int,
    user: CurrentUser = Depends(require_user),
) -> None:
    with _session_factory()() as session:
        row = session.get(BookmarkRecord, bookmark_id)
        if row is None or int(row.user_id) != int(user.user_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="书签不存在")
        session.delete(row)
        session.commit()


# ---------------------------------------------------------------------------
# Read Records + Month Heatmap
# ---------------------------------------------------------------------------


class HeartbeatIn(BaseModel):
    book_key: str = Field(min_length=1, max_length=32)
    book_name: str = Field(default="", max_length=255)
    delta_ms: int = Field(ge=1000, le=3600_000)
    read_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class BookReadRecordOut(BaseModel):
    book_key: str
    book_name: str
    duration_ms: int
    last_read_at: str


class DailyHeatmapPoint(BaseModel):
    date: str
    duration_ms: int


class ReadRecordsSummaryOut(BaseModel):
    total_ms: int
    books: list[BookReadRecordOut]
    daily_heatmap: list[DailyHeatmapPoint]


@router.post("/read-records/heartbeat", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def record_read_heartbeat(
    payload: HeartbeatIn,
    user: CurrentUser = Depends(require_user),
) -> None:
    now = utcnow()
    read_date = payload.read_date or now.strftime("%Y-%m-%d")
    with _session_factory()() as session:
        row = session.get(ReadRecordDaily, (int(user.user_id), payload.book_key, read_date))
        if row is None:
            row = ReadRecordDaily(
                user_id=int(user.user_id),
                book_key=payload.book_key,
                read_date=read_date,
                book_name=payload.book_name,
                duration_ms=int(payload.delta_ms),
                last_read_at=now,
            )
            session.add(row)
        else:
            row.duration_ms = int(row.duration_ms) + int(payload.delta_ms)
            if payload.book_name:
                row.book_name = payload.book_name
            row.last_read_at = now
        session.commit()


@router.get("/read-records", response_model=ReadRecordsSummaryOut)
def list_read_records(
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    user: CurrentUser = Depends(require_user),
) -> ReadRecordsSummaryOut:
    with _session_factory()() as session:
        stmt = select(ReadRecordDaily).where(ReadRecordDaily.user_id == int(user.user_id))
        rows = list(session.execute(stmt).scalars().all())

    by_book: dict[str, dict[str, Any]] = {}
    by_day: dict[str, int] = {}
    total_ms = 0
    for row in rows:
        ms = int(row.duration_ms)
        total_ms += ms
        entry = by_book.get(row.book_key)
        if entry is None:
            by_book[row.book_key] = {
                "book_key": row.book_key,
                "book_name": row.book_name,
                "duration_ms": ms,
                "last_read_at": row.last_read_at,
            }
        else:
            entry["duration_ms"] += ms
            if row.book_name:
                entry["book_name"] = row.book_name
            if row.last_read_at > entry["last_read_at"]:
                entry["last_read_at"] = row.last_read_at
        if month is None or row.read_date.startswith(month):
            by_day[row.read_date] = by_day.get(row.read_date, 0) + ms

    books = sorted(
        by_book.values(),
        key=lambda item: (item["last_read_at"], item["duration_ms"]),
        reverse=True,
    )
    heatmap = [
        DailyHeatmapPoint(date=date_str, duration_ms=ms)
        for date_str, ms in sorted(by_day.items())
    ]
    return ReadRecordsSummaryOut(
        total_ms=total_ms,
        books=[
            BookReadRecordOut(
                book_key=item["book_key"],
                book_name=item["book_name"],
                duration_ms=int(item["duration_ms"]),
                last_read_at=_iso(item["last_read_at"]),
            )
            for item in books
        ],
        daily_heatmap=heatmap,
    )


@router.delete(
    "/read-records/{book_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_book_read_records(
    book_key: str,
    user: CurrentUser = Depends(require_user),
) -> None:
    with _session_factory()() as session:
        session.execute(
            delete(ReadRecordDaily).where(
                ReadRecordDaily.user_id == int(user.user_id),
                ReadRecordDaily.book_key == book_key,
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# Replace Rules
# ---------------------------------------------------------------------------


class ReplaceRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    group: str = Field(default="", max_length=128)
    pattern: str = Field(min_length=1, max_length=4000)
    replacement: str = Field(default="", max_length=4000)
    scope: str = Field(default="", max_length=2000)
    exclude_scope: str = Field(default="", max_length=2000)
    scope_title: bool = False
    scope_content: bool = True
    is_enabled: bool = True
    is_regex: bool = True
    order_index: int = 0


class ReplaceRuleOut(BaseModel):
    id: int
    name: str
    group: str
    pattern: str
    replacement: str
    scope: str
    exclude_scope: str
    scope_title: bool
    scope_content: bool
    is_enabled: bool
    is_regex: bool
    order_index: int
    updated_at: str


class ReplaceRuleTestIn(BaseModel):
    text: str = Field(max_length=50_000)
    pattern: str = Field(min_length=1, max_length=4000)
    replacement: str = Field(default="", max_length=4000)
    is_regex: bool = True


class ReplaceRuleTestOut(BaseModel):
    result: str
    matched: bool
    error: str = ""


def _replace_rule_out(row: ReplaceRuleRecord) -> ReplaceRuleOut:
    return ReplaceRuleOut(
        id=int(row.id),
        name=row.name,
        group=row.group,
        pattern=row.pattern,
        replacement=row.replacement,
        scope=row.scope,
        exclude_scope=row.exclude_scope,
        scope_title=bool(row.scope_title),
        scope_content=bool(row.scope_content),
        is_enabled=bool(row.is_enabled),
        is_regex=bool(row.is_regex),
        order_index=int(row.order_index),
        updated_at=_iso(row.updated_at),
    )


@router.get("/replace-rules", response_model=list[ReplaceRuleOut])
def list_replace_rules(
    group: str | None = Query(default=None, max_length=128),
    user: CurrentUser = Depends(require_user),
) -> list[ReplaceRuleOut]:
    with _session_factory()() as session:
        stmt = select(ReplaceRuleRecord).where(ReplaceRuleRecord.user_id == int(user.user_id))
        if group is not None:
            stmt = stmt.where(ReplaceRuleRecord.group == group)
        stmt = stmt.order_by(ReplaceRuleRecord.order_index.asc(), ReplaceRuleRecord.id.asc())
        rows = list(session.execute(stmt).scalars().all())
    return [_replace_rule_out(row) for row in rows]


@router.post("/replace-rules", response_model=ReplaceRuleOut, status_code=status.HTTP_201_CREATED)
def create_replace_rule(
    payload: ReplaceRuleIn,
    user: CurrentUser = Depends(require_user),
) -> ReplaceRuleOut:
    if payload.is_regex:
        try:
            re.compile(payload.pattern)
        except re.error as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"正则表达式无效：{exc}",
            ) from exc
    with _session_factory()() as session:
        row = ReplaceRuleRecord(
            user_id=int(user.user_id),
            **payload.model_dump(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _replace_rule_out(row)


@router.put("/replace-rules/{rule_id}", response_model=ReplaceRuleOut)
def update_replace_rule(
    rule_id: int,
    payload: ReplaceRuleIn,
    user: CurrentUser = Depends(require_user),
) -> ReplaceRuleOut:
    if payload.is_regex:
        try:
            re.compile(payload.pattern)
        except re.error as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"正则表达式无效：{exc}",
            ) from exc
    with _session_factory()() as session:
        row = session.get(ReplaceRuleRecord, rule_id)
        if row is None or int(row.user_id) != int(user.user_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="替换规则不存在")
        for key, value in payload.model_dump().items():
            setattr(row, key, value)
        row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        return _replace_rule_out(row)


@router.delete(
    "/replace-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_replace_rule(
    rule_id: int,
    user: CurrentUser = Depends(require_user),
) -> None:
    with _session_factory()() as session:
        row = session.get(ReplaceRuleRecord, rule_id)
        if row is None or int(row.user_id) != int(user.user_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="替换规则不存在")
        session.delete(row)
        session.commit()


@router.post("/replace-rules/test", response_model=ReplaceRuleTestOut)
def test_replace_rule(
    payload: ReplaceRuleTestIn,
    user: CurrentUser = Depends(require_user),
) -> ReplaceRuleTestOut:
    _ = user
    if payload.is_regex:
        try:
            result = re.sub(payload.pattern, payload.replacement, payload.text)
        except re.error as exc:
            return ReplaceRuleTestOut(result=payload.text, matched=False, error=str(exc))
    else:
        result = payload.text.replace(payload.pattern, payload.replacement)
    return ReplaceRuleTestOut(result=result, matched=result != payload.text)


# ---------------------------------------------------------------------------
# Explore Pins
# ---------------------------------------------------------------------------


class ExplorePinIn(BaseModel):
    url_id: int = Field(ge=1)
    source_name: str = Field(default="", max_length=255)
    kind_name: str = Field(min_length=1, max_length=255)
    kind_url: str = Field(min_length=1, max_length=2048)
    sort_order: int = 0


class ExplorePinOut(BaseModel):
    id: int
    url_id: int
    source_name: str
    kind_name: str
    kind_url: str
    sort_order: int
    created_at: str


def _pin_out(row: ExplorePinRecord) -> ExplorePinOut:
    return ExplorePinOut(
        id=int(row.id),
        url_id=int(row.url_id),
        source_name=row.source_name,
        kind_name=row.kind_name,
        kind_url=row.kind_url,
        sort_order=int(row.sort_order),
        created_at=_iso(row.created_at),
    )


@router.get("/explore/pins", response_model=list[ExplorePinOut])
def list_explore_pins(user: CurrentUser = Depends(require_user)) -> list[ExplorePinOut]:
    with _session_factory()() as session:
        stmt = (
            select(ExplorePinRecord)
            .where(ExplorePinRecord.user_id == int(user.user_id))
            .order_by(ExplorePinRecord.sort_order.asc(), ExplorePinRecord.id.asc())
        )
        rows = list(session.execute(stmt).scalars().all())
    return [_pin_out(row) for row in rows]


@router.post("/explore/pins", response_model=ExplorePinOut, status_code=status.HTTP_201_CREATED)
def create_explore_pin(
    payload: ExplorePinIn,
    user: CurrentUser = Depends(require_user),
) -> ExplorePinOut:
    with _session_factory()() as session:
        row = ExplorePinRecord(
            user_id=int(user.user_id),
            url_id=payload.url_id,
            source_name=payload.source_name,
            kind_name=payload.kind_name,
            kind_url=payload.kind_url,
            sort_order=payload.sort_order,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _pin_out(row)


@router.delete(
    "/explore/pins/{pin_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_explore_pin(
    pin_id: int,
    user: CurrentUser = Depends(require_user),
) -> None:
    with _session_factory()() as session:
        row = session.get(ExplorePinRecord, pin_id)
        if row is None or int(row.user_id) != int(user.user_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="置顶发现不存在")
        session.delete(row)
        session.commit()


# ---------------------------------------------------------------------------
# User Preferences & Home Layout
# ---------------------------------------------------------------------------


class JsonBlob(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = ""


class JsonBlobIn(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)


def _get_or_default_prefs(user_id: int) -> UserPreferencesRecord | None:
    with _session_factory()() as session:
        return session.get(UserPreferencesRecord, int(user_id))


@router.get("/user/preferences", response_model=JsonBlob)
def get_user_preferences(user: CurrentUser = Depends(require_user)) -> JsonBlob:
    row = _get_or_default_prefs(user.user_id)
    if row is None:
        return JsonBlob()
    return JsonBlob(data=dict(row.preferences_json or {}), updated_at=_iso(row.updated_at))


@router.put("/user/preferences", response_model=JsonBlob)
def put_user_preferences(
    payload: JsonBlobIn,
    user: CurrentUser = Depends(require_user),
) -> JsonBlob:
    with _session_factory()() as session:
        row = session.get(UserPreferencesRecord, int(user.user_id))
        if row is None:
            row = UserPreferencesRecord(
                user_id=int(user.user_id),
                preferences_json=dict(payload.data),
                home_layout_json={},
            )
            session.add(row)
        else:
            row.preferences_json = dict(payload.data)
            row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        return JsonBlob(data=dict(row.preferences_json or {}), updated_at=_iso(row.updated_at))


@router.get("/home/layout", response_model=JsonBlob)
def get_home_layout(user: CurrentUser = Depends(require_user)) -> JsonBlob:
    row = _get_or_default_prefs(user.user_id)
    if row is None:
        return JsonBlob()
    return JsonBlob(data=dict(row.home_layout_json or {}), updated_at=_iso(row.updated_at))


@router.put("/home/layout", response_model=JsonBlob)
def put_home_layout(
    payload: JsonBlobIn,
    user: CurrentUser = Depends(require_user),
) -> JsonBlob:
    with _session_factory()() as session:
        row = session.get(UserPreferencesRecord, int(user.user_id))
        if row is None:
            row = UserPreferencesRecord(
                user_id=int(user.user_id),
                preferences_json={},
                home_layout_json=dict(payload.data),
            )
            session.add(row)
        else:
            row.home_layout_json = dict(payload.data)
            row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        return JsonBlob(data=dict(row.home_layout_json or {}), updated_at=_iso(row.updated_at))
