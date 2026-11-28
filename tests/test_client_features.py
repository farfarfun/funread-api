"""Tests for FunRead Flutter client backend extensions (M1-M3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from funread.legado.engine import BookInfo, Chapter, ChapterContent
from funread_api.app import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_shelf_patch_pin_override_and_batch_delete():
    with _client() as client:
        r1 = client.post(
            "/api/v1/shelf",
            json={"name": "第一本", "author": "甲", "url_id": 1, "book_url": "http://a.test/1"},
        )
        assert r1.status_code == 201
        k1 = r1.json()["book_key"]

        r2 = client.post(
            "/api/v1/shelf",
            json={"name": "第二本", "author": "乙", "url_id": 2, "book_url": "http://b.test/2"},
        )
        assert r2.status_code == 201
        k2 = r2.json()["book_key"]

        # Pin k2 and customize its metadata + group
        patched = client.patch(
            f"/api/v1/shelf/{k2}",
            json={
                "pinned": True,
                "sort_order": 10,
                "can_update": False,
                "name": "自定义书名",
                "group": "追更",
            },
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["pinned"] is True
        assert body["sort_order"] == 10
        assert body["can_update"] is False
        assert body["name"] == "自定义书名"
        assert body["group"] == "追更"

        # Listing orders pinned book first
        shelf = client.get("/api/v1/shelf").json()
        assert shelf[0]["book_key"] == k2
        assert shelf[0]["name"] == "自定义书名"

        # Batch delete both
        deleted = client.post("/api/v1/shelf/batch-delete", json={"book_keys": [k1, k2]})
        assert deleted.status_code == 200
        assert deleted.json()["affected"] == 2
        assert client.get("/api/v1/shelf").json() == []


def test_bookmarks_crud_and_search():
    with _client() as client:
        created = client.post(
            "/api/v1/bookmarks",
            json={
                "book_key": "bk_1",
                "book_name": "诡秘之主",
                "book_author": "爱潜水的乌贼",
                "chapter_index": 3,
                "chapter_name": "第四章 占卜",
                "char_offset": 120,
                "book_text": "绯红的月光照进房间。",
                "content": "伏笔很深",
            },
        )
        assert created.status_code == 201
        bm = created.json()
        bm_id = bm["id"]
        assert bm["book_name"] == "诡秘之主"

        # Filter by query and book_key
        found = client.get("/api/v1/bookmarks", params={"book_key": "bk_1", "q": "伏笔"}).json()
        assert len(found) == 1
        assert found[0]["id"] == bm_id

        # Update note
        updated = client.put(f"/api/v1/bookmarks/{bm_id}", json={"content": "重要伏笔"})
        assert updated.status_code == 200
        assert updated.json()["content"] == "重要伏笔"

        # Delete
        resp = client.delete(f"/api/v1/bookmarks/{bm_id}")
        assert resp.status_code == 204
        assert client.get("/api/v1/bookmarks").json() == []


def test_read_records_heartbeat_heatmap_and_delete():
    with _client() as client:
        r1 = client.post(
            "/api/v1/read-records/heartbeat",
            json={
                "book_key": "bk_a",
                "book_name": "凡人修仙传",
                "delta_ms": 60_000,
                "read_date": "2026-11-01",
            },
        )
        assert r1.status_code == 204

        r2 = client.post(
            "/api/v1/read-records/heartbeat",
            json={
                "book_key": "bk_a",
                "book_name": "凡人修仙传",
                "delta_ms": 30_000,
                "read_date": "2026-11-01",
            },
        )
        assert r2.status_code == 204

        r3 = client.post(
            "/api/v1/read-records/heartbeat",
            json={
                "book_key": "bk_b",
                "book_name": "剑来",
                "delta_ms": 45_000,
                "read_date": "2026-10-15",
            },
        )
        assert r3.status_code == 204

        summary = client.get("/api/v1/read-records", params={"month": "2026-11"}).json()
        assert summary["total_ms"] == 135_000
        assert len(summary["books"]) == 2
        assert summary["daily_heatmap"] == [{"date": "2026-11-01", "duration_ms": 90_000}]

        assert client.delete("/api/v1/read-records/bk_a").status_code == 204
        after = client.get("/api/v1/read-records").json()
        assert after["total_ms"] == 45_000


def test_replace_rules_crud_test_and_batch_chapters_purify(monkeypatch):
    class _FakeService:
        database_url = None

        def content(
            self, url_id: int, chapter: Chapter, info: BookInfo | None = None, book_key: str = ""
        ) -> ChapterContent:
            if chapter.index == 1:
                raise RuntimeError("源站超时")
            return ChapterContent(
                title=f"{chapter.name} [广告]",
                text="正文第一段。请访问 www.bad.test 获取最新章节。",
                url=chapter.url,
                next_url="",
            )

    monkeypatch.setattr("funread_api.v1.reader.get_reader_service", lambda: _FakeService())

    with _client() as client:
        # Invalid regex rejected
        bad = client.post(
            "/api/v1/replace-rules",
            json={"name": "坏正则", "pattern": "[unclosed", "is_regex": True},
        )
        assert bad.status_code == 422

        # Create valid purify rule
        rule = client.post(
            "/api/v1/replace-rules",
            json={
                "name": "去网址广告",
                "group": "净化",
                "pattern": r"请访问\s+www\.bad\.test\s+获取最新章节。",
                "replacement": "",
                "scope_title": False,
                "scope_content": True,
                "is_enabled": True,
                "is_regex": True,
            },
        )
        assert rule.status_code == 201
        rule_id = rule.json()["id"]

        # Test endpoint
        tested = client.post(
            "/api/v1/replace-rules/test",
            json={
                "text": "你好请访问 www.bad.test 获取最新章节。",
                "pattern": r"请访问\s+www\.bad\.test\s+获取最新章节。",
                "replacement": "",
                "is_regex": True,
            },
        )
        assert tested.status_code == 200
        assert tested.json()["matched"] is True
        assert tested.json()["result"] == "你好"

        # Single chapter content gets purified
        single = client.post(
            "/api/v1/reader/content",
            json={
                "url_id": 7,
                "book_key": "k1",
                "chapter": {"index": 0, "name": "第一章", "url": "http://s.test/c/0"},
            },
        )
        assert single.status_code == 200
        assert single.json()["text"] == "正文第一段。"
        assert single.json()["applied_rule_ids"] == [rule_id]

        # Batch chapters tolerates single-chapter failure and purifies successful chapter
        batch = client.post(
            "/api/v1/reader/chapters/batch",
            json={
                "url_id": 7,
                "book_key": "k1",
                "chapters": [
                    {"index": 0, "name": "第一章", "url": "http://s.test/c/0"},
                    {"index": 1, "name": "第二章", "url": "http://s.test/c/1"},
                ],
            },
        )
        assert batch.status_code == 200
        data = batch.json()
        assert data["ok"] == 1
        assert data["failed"] == 1
        assert data["items"][0]["text"] == "正文第一段。"
        assert data["items"][0]["applied_rule_ids"] == [rule_id]
        assert "源站超时" in data["items"][1]["error"]

        # Disable rule & verify update + delete
        upd = client.put(
            f"/api/v1/replace-rules/{rule_id}",
            json={
                "name": "去网址广告",
                "pattern": "www.bad.test",
                "replacement": "",
                "is_enabled": False,
                "is_regex": False,
            },
        )
        assert upd.status_code == 200
        assert upd.json()["is_enabled"] is False
        assert client.delete(f"/api/v1/replace-rules/{rule_id}").status_code == 204


def test_preferences_home_layout_and_explore_pins():
    with _client() as client:
        # Empty defaults
        assert client.get("/api/v1/user/preferences").json()["data"] == {}
        assert client.get("/api/v1/home/layout").json()["data"] == {}

        # Put & get preferences
        p = client.put(
            "/api/v1/user/preferences",
            json={"data": {"theme_mode": 3, "font_size": 20}},
        )
        assert p.status_code == 200
        assert p.json()["data"]["theme_mode"] == 3
        assert client.get("/api/v1/user/preferences").json()["data"]["font_size"] == 20

        # Put & get home layout
        h = client.put(
            "/api/v1/home/layout",
            json={"data": {"tabs": [{"id": "rec", "title": "推荐", "sections": []}]}},
        )
        assert h.status_code == 200
        assert len(client.get("/api/v1/home/layout").json()["data"]["tabs"]) == 1

        # Explore pins
        pin = client.post(
            "/api/v1/explore/pins",
            json={
                "url_id": 12,
                "source_name": "起点测试",
                "kind_name": "玄幻",
                "kind_url": "/xuanhuan/{{page}}",
            },
        )
        assert pin.status_code == 201
        pin_id = pin.json()["id"]
        pins = client.get("/api/v1/explore/pins").json()
        assert len(pins) == 1
        assert pins[0]["kind_name"] == "玄幻"
        assert client.delete(f"/api/v1/explore/pins/{pin_id}").status_code == 204


def test_pool_import_and_debug(monkeypatch):
    @dataclass
    class _Hit:
        name: str
        author: str
        book_url: str

    class _FakeRegistry:
        def __init__(self) -> None:
            self.ingested: list[dict[str, Any]] = []

        def ingest(self, payloads: list[dict[str, Any]]) -> None:
            self.ingested.extend(payloads)

        def scan(self) -> dict[str, int]:
            return {"scanned": len(self.ingested), "enabled": len(self.ingested)}

    class _FakeDebugService:
        def __init__(self) -> None:
            self.registry = _FakeRegistry()

        def search_one(self, url_id: int, keyword: str):
            return [_Hit(name="测试书", author="测试作者", book_url="http://s.test/b/1")]

        def book_info(self, url_id: int, book_url: str, name: str = "", author: str = ""):
            return BookInfo(name=name, author=author, book_url=book_url, toc_url=f"{book_url}/toc")

        def toc(self, url_id: int, info: BookInfo):
            return [Chapter(index=0, name="第一章", url="http://s.test/c/0")]

        def content(self, url_id: int, chapter: Chapter, info: BookInfo | None = None):
            return ChapterContent(
                title=chapter.name,
                text="这是调试出来的正文内容。",
                url=chapter.url,
                next_url="",
            )

    fake_service = _FakeDebugService()
    monkeypatch.setattr("funread_api.v1.pool.get_reader_service", lambda: fake_service)

    with _client() as client:
        imp = client.post(
            "/api/v1/pool/import",
            json={
                "source_type": "book",
                "sources": [{"bookSourceName": "测试源", "bookSourceUrl": "http://s.test"}],
            },
        )
        assert imp.status_code == 201
        assert imp.json() == {"imported": 1, "scanned": 1, "enabled": 1}

        dbg = client.post("/api/v1/pool/debug", json={"url_id": 1, "keyword": "测试"})
        assert dbg.status_code == 200
        body = dbg.json()
        assert body["ok"] is True
        assert [s["stage"] for s in body["steps"]] == ["search", "book_info", "toc", "content"]
        assert all(s["ok"] for s in body["steps"])
