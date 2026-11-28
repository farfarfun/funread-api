"""Client-facing per-user persistence for FunRead Flutter client.

All tables share funread's SQLAlchemy sync engine / database URL so they work
transparently across SQLite (local/tests) and PostgreSQL (production) without
requiring changes to the upstream ``funread`` wheel.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    delete,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from funread.base import config
from funread.legado.manage.source.storage import (
    _DB_INIT_LOCK,
    _get_engine,
    _get_session_factory,
    utcnow,
)


class ClientDataBase(DeclarativeBase):
    """Declarative base for FunRead client-extension tables."""


class ShelfBookMeta(ClientDataBase):
    """Per-user shelf overrides: pinning, sort order, update toggle, custom metadata."""

    __tablename__ = "client_shelf_meta"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    can_update: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    custom_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    custom_author: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    custom_cover_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    custom_intro: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class BookmarkRecord(ClientDataBase):
    """Reading bookmarks with excerpt and user note, scoped per user."""

    __tablename__ = "client_bookmarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    book_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    book_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    book_author: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    chapter_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chapter_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    char_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    book_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ReadRecordDaily(ClientDataBase):
    """Per-user per-book daily reading duration for stats and month heatmap."""

    __tablename__ = "client_read_records_daily"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    read_date: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD
    book_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_read_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ReplaceRuleRecord(ClientDataBase):
    """Content purification replace rules, matching funread-app ReplaceRule."""

    __tablename__ = "client_replace_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    group: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    pattern: Mapped[str] = mapped_column(Text, nullable=False, default="")
    replacement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="")
    exclude_scope: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scope_title: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scope_content: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_regex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ExplorePinRecord(ClientDataBase):
    """Pinned explore categories for quick access in ExploreScreen / HomeScreen."""

    __tablename__ = "client_explore_pins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    url_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    kind_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    kind_url: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class UserPreferencesRecord(ClientDataBase):
    """Per-user JSON snapshots for theme/reader preferences and custom home layout."""

    __tablename__ = "client_user_preferences"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    preferences_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    home_layout_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


_INITIALIZED_URLS: set[str] = set()


def _resolve_db_url(database_url: str | None = None) -> str:
    if database_url:
        return database_url
    return config.resolve_database_url()


def init_client_data_db(database_url: str | None = None) -> None:
    """Create client-extension tables idempotently for the active database URL."""
    url = _resolve_db_url(database_url)
    if url in _INITIALIZED_URLS:
        return
    with _DB_INIT_LOCK:
        if url in _INITIALIZED_URLS:
            return
        engine = _get_engine(url)
        ClientDataBase.metadata.create_all(engine)
        _INITIALIZED_URLS.add(url)


def reset_client_data_init_cache() -> None:
    with _DB_INIT_LOCK:
        _INITIALIZED_URLS.clear()


def _session_factory(database_url: str | None = None):
    url = _resolve_db_url(database_url)
    init_client_data_db(url)
    return _get_session_factory(url)


# ---------------------------------------------------------------------------
# Shelf metadata helpers
# ---------------------------------------------------------------------------


def get_shelf_meta_map(
    user_id: int, database_url: str | None = None
) -> dict[str, ShelfBookMeta]:
    with _session_factory(database_url)() as session:
        rows = (
            session.execute(
                select(ShelfBookMeta).where(ShelfBookMeta.user_id == int(user_id))
            )
            .scalars()
            .all()
        )
        return {row.book_key: row for row in rows}


def upsert_shelf_meta(
    user_id: int,
    book_key: str,
    *,
    pinned: bool | None = None,
    sort_order: int | None = None,
    can_update: bool | None = None,
    custom_name: str | None = None,
    custom_author: str | None = None,
    custom_cover_url: str | None = None,
    custom_intro: str | None = None,
    database_url: str | None = None,
) -> ShelfBookMeta:
    with _session_factory(database_url)() as session:
        row = session.get(ShelfBookMeta, (int(user_id), book_key))
        if row is None:
            row = ShelfBookMeta(user_id=int(user_id), book_key=book_key)
            session.add(row)
        if pinned is not None:
            row.pinned = bool(pinned)
        if sort_order is not None:
            row.sort_order = int(sort_order)
        if can_update is not None:
            row.can_update = bool(can_update)
        if custom_name is not None:
            row.custom_name = custom_name
        if custom_author is not None:
            row.custom_author = custom_author
        if custom_cover_url is not None:
            row.custom_cover_url = custom_cover_url
        if custom_intro is not None:
            row.custom_intro = custom_intro
        row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        return row


def delete_shelf_meta(
    user_id: int, book_keys: list[str], database_url: str | None = None
) -> None:
    if not book_keys:
        return
    with _session_factory(database_url)() as session:
        session.execute(
            delete(ShelfBookMeta).where(
                ShelfBookMeta.user_id == int(user_id),
                ShelfBookMeta.book_key.in_(book_keys),
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# Replace rule evaluation
# ---------------------------------------------------------------------------


def _matches_scope(scope: str, book_name: str, book_key: str) -> bool:
    tokens = [token.strip() for token in scope.split(",") if token.strip()]
    if not tokens:
        return True
    targets = {item for item in (book_name.strip(), book_key.strip()) if item}
    return any(token in targets for token in tokens)


def apply_replace_rules(
    text: str,
    rules: list[ReplaceRuleRecord],
    *,
    book_name: str = "",
    book_key: str = "",
    is_title: bool = False,
) -> tuple[str, list[int]]:
    """Apply enabled replace rules in order, returning purified text and hit rule IDs."""
    if not text or not rules:
        return text, []
    result = text
    applied: list[int] = []
    for rule in rules:
        if not rule.is_enabled or not rule.pattern:
            continue
        if is_title and not rule.scope_title:
            continue
        if not is_title and not rule.scope_content:
            continue
        if rule.scope and not _matches_scope(rule.scope, book_name, book_key):
            continue
        if rule.exclude_scope and _matches_scope(rule.exclude_scope, book_name, book_key):
            continue
        next_text = result
        if rule.is_regex:
            try:
                next_text = re.sub(rule.pattern, rule.replacement, result)
            except re.error:
                continue
        else:
            next_text = result.replace(rule.pattern, rule.replacement)
        if next_text != result:
            result = next_text
            applied.append(int(rule.id))
    return result, applied


def list_enabled_replace_rules(
    user_id: int, database_url: str | None = None
) -> list[ReplaceRuleRecord]:
    with _session_factory(database_url)() as session:
        stmt = (
            select(ReplaceRuleRecord)
            .where(
                ReplaceRuleRecord.user_id == int(user_id),
                ReplaceRuleRecord.is_enabled.is_(True),
            )
            .order_by(ReplaceRuleRecord.order_index.asc(), ReplaceRuleRecord.id.asc())
        )
        return list(session.execute(stmt).scalars().all())
