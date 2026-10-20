"""Reader and shelf routes, driven against a fake fetcher — nothing hits the network."""

import json
import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from funread.base import config
from funread.legado.engine import StaticFetcher
from funread.legado.manage.source.storage import add_source_list_url, upsert_source_detail_record
from funread.legado.reader import get_cached_chapter, save_cached_chapter
from funread.legado.reader import storage as reader_storage
from funread_api.app import create_app
from funread_api.v1.deps import get_reader_service, reset_reader_services

SOURCE = {
    "bookSourceUrl": "https://a.example.com",
    "bookSourceName": "甲源",
    "searchUrl": "/s?q={{key}}",
    "ruleSearch": {
        "bookList": "class.r@tag.li",
        "name": "class.n@text",
        "author": "class.a@text",
        "bookUrl": "tag.a@href",
    },
    "ruleBookInfo": {"intro": "id.intro@text", "tocUrl": "id.toc@tag.a@href"},
    "ruleToc": {"chapterList": "id.l@tag.a", "chapterName": "text", "chapterUrl": "href"},
    "ruleContent": {"content": "id.c@text"},
}

PAGES = {
    "https://a.example.com/s?q=剑来": (
        '<ul class="r"><li><span class="n">剑来</span>'
        '<span class="a">烽火戏诸侯</span><a href="/b/1">去</a></li></ul>'
    ),
    "https://a.example.com/b/1": (
        '<div id="intro">少年抱剑</div><div id="toc"><a href="/b/1/toc">目录</a></div>'
    ),
    "https://a.example.com/b/1/toc": (
        '<div id="l"><a href="/c/1">第一章</a><a href="/c/2">第二章</a></div>'
    ),
    "https://a.example.com/c/1": '<div id="c">第一章的正文</div>',
    "https://a.example.com/c/2": '<div id="c">第二章的正文</div>',
}


@pytest.fixture
def client(tmp_path):
    """App wired to a one-source archive and an offline fetcher."""
    hubs = tmp_path / "hubs"
    path = hubs / "book" / "source" / "0-100" / "1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"url_id": 1, "status": 2, "candidate": [{"source": SOURCE}]}),
        encoding="utf-8",
    )
    config.set_overrides(database_url=f"sqlite:///{tmp_path / 'reader.db'}", cache_root=str(hubs))

    reset_reader_services()
    with TestClient(create_app()) as test_client:
        service = get_reader_service()
        service.fetcher_factory = lambda timeout=None: StaticFetcher(PAGES)
        service.registry.import_archive()
        test_client.service = service
        yield test_client
    reset_reader_services()


def _search_hit(client):
    page = client.get("/api/v1/reader/search", params={"keyword": "剑来"}).json()
    return page["items"][0]


# ------------------------------------------------------------------ 四段流程


def test_scan_reports_what_it_enabled(client):
    report = client.post("/api/v1/reader/scan").json()

    assert report == {
        "source_type": "book",
        "scanned": 1,
        "complete": 1,
        "needs_js": 0,
        "web_view": 0,
        "has_explore": 0,
        "enabled": 1,
    }


def test_the_two_source_types_have_separate_pools(client, monkeypatch):
    """rss 的候选池是另一次扫描，不是 book 那次的副作用。"""
    monkeypatch.setattr(
        "funread_api.v1.reader.fetch_source_list_data",
        lambda record: [
            [
                {
                    "sourceUrl": "https://feed.example.com",
                    "sourceName": "测试订阅源",
                    "ruleArticles": "class.item",
                    "ruleTitle": "tag.h2@text",
                    "ruleLink": "tag.a@href",
                    "ruleContent": "class.body@html",
                }
            ]
        ],
    )
    book = client.post("/api/v1/reader/scan").json()
    rss = client.post("/api/v1/reader/scan", params={"source_type": "rss"}).json()

    assert book["enabled"] == 1
    #  夹具没有 RSS 归档，扫描会从内置列表初始化独立的 RSS 池。
    assert rss == {
        "source_type": "rss",
        "scanned": 1,
        "complete": 1,
        "needs_js": 0,
        "web_view": 0,
        "has_explore": 0,
        "enabled": 1,
    }


def test_scan_reports_list_download_failure(client, monkeypatch):
    def fail(_record):
        raise RuntimeError("download failed")

    monkeypatch.setattr("funread_api.v1.reader.fetch_source_list_data", fail)
    response = client.post("/api/v1/reader/scan", params={"source_type": "rss"})
    assert response.status_code == 502
    assert "download failed" in response.json()["detail"]


def test_scan_skips_incremental_lists_when_bootstrapping(client, monkeypatch):
    add_source_list_url(
        url="https://example.com/rss/{id}.json",
        source_type="rss",
        increment_start=0,
        increment_stop=1000,
    )
    fetched = []

    def fetch(record):
        fetched.append(record.url)
        return [
            [
                {
                    "sourceUrl": "https://feed.example.com",
                    "sourceName": "测试订阅源",
                    "ruleArticles": "class.item",
                    "ruleTitle": "tag.h2@text",
                }
            ]
        ]

    monkeypatch.setattr("funread_api.v1.reader.fetch_source_list_data", fetch)
    report = client.post("/api/v1/reader/scan", params={"source_type": "rss"}).json()

    assert report["scanned"] == 1
    assert fetched
    assert all("{id}" not in url for url in fetched)


def test_scan_rejects_an_unknown_source_type(client):
    assert client.post("/api/v1/reader/scan", params={"source_type": "video"}).status_code == 422


def test_search_returns_the_book_with_its_sources(client):
    response = client.get("/api/v1/reader/search", params={"keyword": "剑来"})

    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["name"] == "剑来"
    assert page["items"][0]["sources"] == [
        {"url_id": 1, "source_name": "甲源", "book_url": "https://a.example.com/b/1"}
    ]


def test_search_without_results_is_an_empty_page_not_an_error(client):
    """Every candidate source failing is the normal case, not a 500."""
    response = client.get("/api/v1/reader/search", params={"keyword": "查无此书"})

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    #  The counts are what lets the UI say "all sources failed" rather than
    #  showing an empty list that looks like "no such book".
    assert body["sources_tried"] >= 1
    assert body["sources_ok"] == 0
    assert body["failed"] >= 1


def test_search_reports_how_the_fan_out_went(client):
    body = client.get("/api/v1/reader/search", params={"keyword": "剑来"}).json()

    assert body["sources_ok"] >= 1
    assert body["sources_tried"] >= body["sources_ok"]
    assert body["js_skipped"] == 0


def test_search_offset_pages_through_the_round(client):
    first = client.get(
        "/api/v1/reader/search", params={"keyword": "剑来", "limit": 1, "offset": 0}
    ).json()
    assert first["limit"] == 1
    assert first["offset"] == 0
    assert len(first["items"]) <= 1

    beyond = client.get(
        "/api/v1/reader/search", params={"keyword": "剑来", "limit": 1, "offset": 999}
    ).json()
    #  Past the end is an empty window, not an error -- and total still tells
    #  the client where the end was.
    assert beyond["items"] == []
    assert beyond["total"] == first["total"]


def test_book_toc_and_content_chain_together(client):
    hit = _search_hit(client)

    info = client.get(
        "/api/v1/reader/book",
        params={"url_id": 1, "book_url": hit["sources"][0]["book_url"], "name": hit["name"]},
    ).json()
    assert info["intro"] == "少年抱剑"

    toc = client.post("/api/v1/reader/toc", json={"url_id": 1, "book": info}).json()
    assert toc["total"] == 2
    assert toc["items"][0]["name"] == "第一章"

    content = client.post(
        "/api/v1/reader/content", json={"url_id": 1, "chapter": toc["items"][0]}
    ).json()
    assert content["text"] == "第一章的正文"


def test_unknown_source_is_a_404(client):
    response = client.post(
        "/api/v1/reader/content",
        json={"url_id": 999999, "chapter": {"index": 1, "url": "https://a.example.com/c/1"}},
    )

    assert response.status_code == 404


def test_a_dead_chapter_url_is_a_502_not_a_500(client):
    """The source is fine, the site misbehaved — the UI should offer retry."""
    response = client.post(
        "/api/v1/reader/content",
        json={"url_id": 1, "chapter": {"index": 9, "url": "https://a.example.com/c/9"}},
    )

    assert response.status_code == 502


# ------------------------------------------------------------------ 书架


def test_shelf_round_trip(client):
    created = client.post(
        "/api/v1/shelf",
        json={"name": "剑来", "author": "烽火戏诸侯", "url_id": 1, "book_url": "https://a/b/1"},
    )
    assert created.status_code == 201
    book_key = created.json()["book_key"]

    assert (
        client.put(
            f"/api/v1/shelf/{book_key}/progress",
            json={"chapter_index": 3, "chapter_name": "第三章", "char_offset": 120},
        ).status_code
        == 204
    )

    shelf = client.get("/api/v1/shelf").json()
    assert len(shelf) == 1
    assert shelf[0]["name"] == "剑来"
    assert shelf[0]["progress"] == {
        "chapter_index": 3,
        "chapter_name": "第三章",
        "char_offset": 120,
    }

    assert client.delete(f"/api/v1/shelf/{book_key}").status_code == 204
    assert client.get("/api/v1/shelf").json() == []


def test_shelf_add_is_idempotent(client):
    first = client.post("/api/v1/shelf", json={"name": "剑来", "author": "烽火戏诸侯"}).json()
    second = client.post("/api/v1/shelf", json={"name": "剑来", "author": "烽火戏诸侯"}).json()

    assert first["book_key"] == second["book_key"]
    assert len(client.get("/api/v1/shelf").json()) == 1


def test_progress_on_a_book_not_on_the_shelf_is_a_404(client):
    response = client.put("/api/v1/shelf/deadbeef/progress", json={"chapter_index": 1})

    assert response.status_code == 404


def test_removing_a_missing_book_is_a_404(client):
    assert client.delete("/api/v1/shelf/deadbeef").status_code == 404


# ------------------------------------------------------------------ 缓存/离线


def test_cached_content_is_served_without_fetching(client):
    book_key = client.post("/api/v1/shelf", json={"name": "剑来"}).json()["book_key"]
    save_cached_chapter(
        book_key,
        1,
        "https://a.example.com/c/1",
        "第一章",
        "缓存正文",
        database_url=client.service.database_url,
    )

    content = client.post(
        "/api/v1/reader/content",
        json={
            "url_id": 1,
            "chapter": {"index": 1, "url": "https://a.example.com/c/1"},
            "book_key": book_key,
        },
    ).json()

    assert content["text"] == "缓存正文"


def test_download_fills_the_cache(client):
    book_key = client.post("/api/v1/shelf", json={"name": "剑来"}).json()["book_key"]

    accepted = client.post(
        f"/api/v1/shelf/{book_key}/download",
        json={
            "url_id": 1,
            "interval": 0,
            "chapters": [
                {"index": 1, "name": "第一章", "url": "https://a.example.com/c/1"},
                {"index": 2, "name": "第二章", "url": "https://a.example.com/c/2"},
            ],
        },
    )

    assert accepted.status_code == 202
    body = accepted.json()
    assert body["book_key"] == book_key
    assert body["queued"] == 2
    task_id = body["task_id"]
    assert task_id

    #  TestClient drains background tasks before returning, so the work is done
    cached = client.get(f"/api/v1/shelf/{book_key}/cached").json()
    assert cached["chapter_indexes"] == [1, 2]
    #  Finished, so nothing is in flight any more
    assert cached["downloading"] is None

    progress = client.get(f"/api/v1/shelf/{book_key}/download/{task_id}").json()
    assert progress["state"] == "done"
    assert progress["total"] == 2
    assert progress["done"] == 2
    assert progress["failed"] == 0

    assert client.delete(f"/api/v1/shelf/{book_key}/cached").json()["chapter_indexes"] == []


def test_download_progress_for_an_unknown_task_is_a_404(client):
    book_key = client.post("/api/v1/shelf", json={"name": "剑来"}).json()["book_key"]

    assert client.get(f"/api/v1/shelf/{book_key}/download/nope").status_code == 404


def test_a_failing_download_is_reported_not_left_spinning(client, monkeypatch):
    """The task runs after the response; an unhandled error would only hit the log."""
    from funread_api.v1 import deps

    book_key = client.post("/api/v1/shelf", json={"name": "剑来"}).json()["book_key"]
    service = deps.get_reader_service()

    def _boom(*args, **kwargs):
        raise RuntimeError("源站炸了")

    monkeypatch.setattr(service, "download_chapters", _boom)

    accepted = client.post(
        f"/api/v1/shelf/{book_key}/download",
        json={
            "url_id": 1,
            "interval": 0,
            "chapters": [{"index": 1, "name": "第一章", "url": "https://a.example.com/c/1"}],
        },
    )
    task_id = accepted.json()["task_id"]

    progress = client.get(f"/api/v1/shelf/{book_key}/download/{task_id}").json()
    assert progress["state"] == "error"
    assert "源站炸了" in progress["detail"]


def test_download_rejects_an_oversized_batch(client):
    """Serial + throttled: a whole 3,000-chapter book would pin a worker for an hour."""
    book_key = client.post("/api/v1/shelf", json={"name": "剑来"}).json()["book_key"]

    response = client.post(
        f"/api/v1/shelf/{book_key}/download",
        json={
            "url_id": 1,
            "chapters": [{"index": i, "url": f"https://a.example.com/c/{i}"} for i in range(201)],
        },
    )

    assert response.status_code == 422


def test_switching_source_drops_the_cache(client):
    """Chapter numbering differs per site, so a stale cache would mix chapters."""
    book_key = client.post("/api/v1/shelf", json={"name": "剑来", "url_id": 1}).json()["book_key"]
    save_cached_chapter(
        book_key, 1, "u", "第一章", "甲源的正文", database_url=client.service.database_url
    )

    response = client.post(
        f"/api/v1/shelf/{book_key}/source",
        json={"url_id": 2, "book_url": "https://b.example.com/b/9"},
    )

    assert response.status_code == 200
    assert get_cached_chapter(book_key, 1, database_url=client.service.database_url) is None


def test_switching_without_progress_skips_the_remap(client):
    """还没开始读，没有进度可搬 —— 不该为此去抓一次目录。"""
    book_key = client.post("/api/v1/shelf", json={"name": "剑来", "url_id": 1}).json()["book_key"]

    body = client.post(
        f"/api/v1/shelf/{book_key}/source",
        json={"url_id": 1, "book_url": "https://a.example.com/b/1"},
    ).json()

    assert body["method"] == "skipped"


def test_switching_source_relocates_the_progress_by_chapter_name(client):
    """换源后进度必须跟着走 —— 否则书架上的章节号指向新源里的别的内容。"""
    book_key = client.post(
        "/api/v1/shelf",
        json={"name": "剑来", "url_id": 1, "book_url": "https://a.example.com/b/1"},
    ).json()["book_key"]
    #  这个假源的目录是「第一章」「第二章」，先把进度放在第二章
    client.put(
        f"/api/v1/shelf/{book_key}/progress",
        json={"chapter_index": 1, "chapter_name": "第二章", "char_offset": 1234},
    )

    body = client.post(
        f"/api/v1/shelf/{book_key}/source",
        json={"url_id": 1, "book_url": "https://a.example.com/b/1"},
    ).json()

    assert body["method"] == "exact"
    assert body["chapter_index"] == 1
    assert body["chapter_name"] == "第二章"
    assert body["is_approximate"] is False
    #  字符偏移不能跨源沿用 —— 新源这一章的长度和分段都不一样
    progress = client.get("/api/v1/shelf").json()[0]["progress"]
    assert progress["chapter_index"] == 1
    assert progress["char_offset"] == 0


def test_a_failed_toc_fetch_reports_none_instead_of_failing_the_switch(client):
    """源已经换了（本地写库）。一次抓取失败不该变成整个换源失败。"""
    book_key = client.post(
        "/api/v1/shelf",
        json={"name": "剑来", "url_id": 1, "book_url": "https://a.example.com/b/1"},
    ).json()["book_key"]
    client.put(
        f"/api/v1/shelf/{book_key}/progress",
        json={"chapter_index": 1, "chapter_name": "第二章"},
    )

    response = client.post(
        f"/api/v1/shelf/{book_key}/source",
        #  StaticFetcher 里没有这个 URL，所以取目录必然失败
        json={"url_id": 1, "book_url": "https://a.example.com/does-not-exist"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "none"
    #  进度原样保留，让用户自己选章
    assert body["chapter_index"] == 1
    assert client.get("/api/v1/shelf").json()[0]["url_id"] == 1


def test_switch_candidates_include_near_matches_and_stats(client):
    """按 book_key 精确筛会静默丢掉作者名写法不同的源 —— 那些往往恰恰还活着。"""
    book_key = client.post(
        "/api/v1/shelf",
        json={
            "name": "剑来",
            "author": "烽火戏诸侯",
            "url_id": 1,
            "book_url": "https://a.example.com/b/1",
        },
    ).json()["book_key"]

    body = client.get("/api/v1/reader/sources", params={"book_key": book_key}).json()

    assert body["book_key"] == book_key
    assert body["name"] == "剑来"
    #  统计字段在，界面才能解释「为什么换源列表是空的」
    for field in ("sources_tried", "sources_ok", "hits", "exhausted", "stopped_by"):
        assert field in body
    #  这个夹具只有一个源，它就是当前源
    assert [item["url_id"] for item in body["items"]] == [1]
    assert body["items"][0]["current"] is True
    assert body["items"][0]["exact"] is True
    assert body["items"][0]["author"] == "烽火戏诸侯"


def test_switch_candidates_for_a_book_not_on_the_shelf_is_a_404(client):
    assert client.get("/api/v1/reader/sources", params={"book_key": "不存在"}).status_code == 404


def test_search_reports_how_it_stopped(client):
    """exhausted 为真才说明「没搜到」是确定结论，而不是还没搜那么深。"""
    body = client.get("/api/v1/reader/search", params={"keyword": "查无此书"}).json()

    assert body["exhausted"] is True
    assert body["stopped_by"] == "exhausted"
    assert body["waves"] == 1
    assert body["hits"] == 0
    assert body["elapsed"] >= 0


# ------------------------------------------------------------------ 书架分组


def _add(client, name, **extra):
    return client.post("/api/v1/shelf", json={"name": name, **extra}).json()["book_key"]


def test_a_new_book_is_ungrouped(client):
    _add(client, "剑来")

    assert client.get("/api/v1/shelf").json()[0]["group"] == ""
    #  未分组也是一组，否则界面上这些书没有落脚的地方
    assert client.get("/api/v1/shelf/groups").json() == [{"name": "", "count": 1}]


def test_moving_books_into_a_group_and_filtering_by_it(client):
    jian = _add(client, "剑来")
    xian = _add(client, "仙逆")

    moved = client.post("/api/v1/shelf/groups/assign", json={"book_keys": [jian], "group": "玄幻"})
    assert moved.status_code == 200
    assert moved.json() == {"affected": 1}

    #  不传 group 是整个书架
    assert len(client.get("/api/v1/shelf").json()) == 2
    #  传了就只看这一组
    grouped = client.get("/api/v1/shelf", params={"group": "玄幻"}).json()
    assert [item["book_key"] for item in grouped] == [jian]
    #  传空串 = 只看未分组的，这是「空串当成没传」最容易错的地方
    ungrouped = client.get("/api/v1/shelf", params={"group": ""}).json()
    assert [item["book_key"] for item in ungrouped] == [xian]


def test_group_listing_counts_each_group(client):
    jian = _add(client, "剑来")
    _add(client, "仙逆")
    client.post("/api/v1/shelf/groups/assign", json={"book_keys": [jian], "group": "玄幻"})

    #  未分组那组排在最后，界面直接按顺序渲染
    assert client.get("/api/v1/shelf/groups").json() == [
        {"name": "玄幻", "count": 1},
        {"name": "", "count": 1},
    ]


def test_re_adding_a_book_keeps_the_group_it_was_put_in(client):
    """加书是幂等的 —— 从搜索结果里再点一次不该把书踢回未分组。"""
    book_key = _add(client, "剑来", author="烽火戏诸侯")
    client.post("/api/v1/shelf/groups/assign", json={"book_keys": [book_key], "group": "玄幻"})

    client.post("/api/v1/shelf", json={"name": "剑来", "author": "烽火戏诸侯"})

    assert client.get("/api/v1/shelf").json()[0]["group"] == "玄幻"


def test_adding_a_book_straight_into_a_group(client):
    client.post("/api/v1/shelf", json={"name": "剑来", "group": "玄幻"})

    assert client.get("/api/v1/shelf").json()[0]["group"] == "玄幻"


def test_moving_a_book_out_of_its_group(client):
    book_key = _add(client, "剑来")
    client.post("/api/v1/shelf/groups/assign", json={"book_keys": [book_key], "group": "玄幻"})

    client.post("/api/v1/shelf/groups/assign", json={"book_keys": [book_key], "group": ""})

    assert client.get("/api/v1/shelf").json()[0]["group"] == ""


def test_renaming_a_group(client):
    book_key = _add(client, "剑来")
    client.post("/api/v1/shelf/groups/assign", json={"book_keys": [book_key], "group": "玄幻"})

    body = client.post("/api/v1/shelf/groups/rename", json={"old": "玄幻", "new": "仙侠"})

    assert body.json() == {"affected": 1}
    assert client.get("/api/v1/shelf").json()[0]["group"] == "仙侠"


def test_dissolving_a_group_sends_its_books_back_to_ungrouped(client):
    book_key = _add(client, "剑来")
    client.post("/api/v1/shelf/groups/assign", json={"book_keys": [book_key], "group": "玄幻"})

    client.post("/api/v1/shelf/groups/rename", json={"old": "玄幻", "new": ""})

    assert client.get("/api/v1/shelf").json()[0]["group"] == ""
    assert client.get("/api/v1/shelf/groups").json() == [{"name": "", "count": 1}]


def test_renaming_a_group_nobody_has_is_a_404(client):
    _add(client, "剑来")

    assert (
        client.post("/api/v1/shelf/groups/rename", json={"old": "玄幻", "new": "仙侠"}).status_code
        == 404
    )


def test_the_ungrouped_bucket_cannot_be_renamed(client):
    """「给所有未分组的书起个名」是批量移动，不是重命名 —— 混在一起一次误操作就
    能把整个书架扫进一个组。"""
    _add(client, "剑来")

    assert (
        client.post("/api/v1/shelf/groups/rename", json={"old": "", "new": "玄幻"}).status_code
        == 422
    )


def test_assigning_with_an_empty_book_list_is_rejected(client):
    assert (
        client.post(
            "/api/v1/shelf/groups/assign", json={"book_keys": [], "group": "玄幻"}
        ).status_code
        == 422
    )


def test_assigning_a_book_that_is_not_on_the_shelf_moves_nothing(client):
    _add(client, "剑来")

    body = client.post(
        "/api/v1/shelf/groups/assign", json={"book_keys": ["不存在"], "group": "玄幻"}
    ).json()

    assert body == {"affected": 0}


# ------------------------------------------------------------------ 检查更新


def test_checking_updates_fills_in_the_chapter_count(client):
    _add(client, "剑来", url_id=1, book_url="https://a.example.com/b/1")

    accepted = client.post("/api/v1/shelf/check-updates", json={"interval": 0})

    assert accepted.status_code == 202
    assert accepted.json()["queued"] == 1
    book = client.get("/api/v1/shelf").json()[0]
    #  这个假源的目录就是两章
    assert book["chapter_count"] == 2
    #  还没读过，所以两章都是未读
    assert book["unread"] == 2
    assert book["last_checked_at"]
    assert book["last_check_error"] == ""


def test_the_unread_badge_follows_the_reading_progress(client):
    book_key = _add(client, "剑来", url_id=1, book_url="https://a.example.com/b/1")
    client.post("/api/v1/shelf/check-updates", json={"interval": 0})

    client.put(f"/api/v1/shelf/{book_key}/progress", json={"chapter_index": 1})

    assert client.get("/api/v1/shelf").json()[0]["unread"] == 0


def test_a_book_with_no_remembered_source_is_recorded_as_a_failure(client):
    """返回 0 会被记成「这本书没有章节」，未读角标跟着清零 —— 必须是失败。"""
    _add(client, "剑来")

    accepted = client.post("/api/v1/shelf/check-updates", json={"interval": 0}).json()

    progress = client.get(f"/api/v1/shelf/check-updates/{accepted['task_id']}").json()
    assert progress["state"] == "done"
    assert progress["failed"] == 1
    book = client.get("/api/v1/shelf").json()[0]
    assert book["chapter_count"] == 0
    assert book["last_check_error"]


def test_check_progress_is_queryable_by_task_id(client):
    _add(client, "剑来", url_id=1, book_url="https://a.example.com/b/1")
    task_id = client.post("/api/v1/shelf/check-updates", json={"interval": 0}).json()["task_id"]

    progress = client.get(f"/api/v1/shelf/check-updates/{task_id}").json()

    assert progress == {
        "task_id": task_id,
        "state": "done",
        "total": 1,
        "done": 1,
        "failed": 0,
        "detail": "",
    }


def test_checking_only_the_books_you_asked_for(client):
    jian = _add(client, "剑来", url_id=1, book_url="https://a.example.com/b/1")
    _add(client, "仙逆", url_id=1, book_url="https://a.example.com/b/1")

    accepted = client.post(
        "/api/v1/shelf/check-updates", json={"book_keys": [jian], "interval": 0}
    ).json()

    assert accepted["queued"] == 1
    checked = {item["name"]: item["chapter_count"] for item in client.get("/api/v1/shelf").json()}
    assert checked == {"剑来": 2, "仙逆": 0}


def test_checking_an_empty_shelf_is_rejected(client):
    assert client.post("/api/v1/shelf/check-updates", json={"interval": 0}).status_code == 422


def test_a_second_round_while_one_is_running_is_a_409(client):
    """两轮并行只会把同一批源打两遍。"""
    from funread_api.v1.deps import get_update_check_tracker

    _add(client, "剑来", url_id=1, book_url="https://a.example.com/b/1")
    #  手动占住 —— TestClient 会把后台任务跑完才返回，没法自然造出「正在跑」
    running = get_update_check_tracker().start("", 0, 1)

    response = client.post("/api/v1/shelf/check-updates", json={"interval": 0})

    assert response.status_code == 409
    assert running.task_id in response.json()["detail"]


def test_an_unknown_check_task_is_a_404(client):
    assert client.get("/api/v1/shelf/check-updates/nope").status_code == 404


# ------------------------------------------------------------------ 发现页


EXPLORE_SOURCE = {
    **SOURCE,
    "bookSourceUrl": "https://e.example.com",
    "bookSourceName": "分类源",
    "exploreUrl": "玄幻::/list/1\n都市::/list/2",
    "ruleExplore": {
        "bookList": "class.r@tag.li",
        "name": "class.n@text",
        "author": "class.a@text",
        "bookUrl": "tag.a@href",
    },
}

EXPLORE_PAGES = {
    "https://e.example.com/list/1": (
        '<ul class="r"><li><span class="n">剑来</span>'
        '<span class="a">烽火戏诸侯</span><a href="/b/1">去</a></li></ul>'
    ),
}


@pytest.fixture
def explore_client(tmp_path):
    """归档里放一个带发现页规则的源。"""
    hubs = tmp_path / "hubs"
    path = hubs / "book" / "source" / "0-100" / "7.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"url_id": 7, "status": 2, "candidate": [{"source": EXPLORE_SOURCE}]}),
        encoding="utf-8",
    )
    config.set_overrides(database_url=f"sqlite:///{tmp_path / 'explore.db'}", cache_root=str(hubs))

    reset_reader_services()
    with TestClient(create_app()) as test_client:
        service = get_reader_service()
        service.fetcher_factory = lambda timeout=None: StaticFetcher(EXPLORE_PAGES)
        service.registry.import_archive()
        yield test_client
    reset_reader_services()


def test_explore_sources_lists_the_browsable_ones(explore_client):
    page = explore_client.get("/api/v1/reader/explore/sources").json()

    assert page["total"] == 1
    assert page["items"][0]["url_id"] == 7
    assert page["items"][0]["kinds"] == ["玄幻", "都市"]


def test_explore_sources_is_empty_when_no_source_declares_them(client):
    """主夹具的源只能搜，不能浏览 —— 不该出现在发现页里。"""
    assert client.get("/api/v1/reader/explore/sources").json()["total"] == 0


def test_explore_kinds_returns_opaque_tokens(explore_client):
    kinds = explore_client.get("/api/v1/reader/explore/kinds", params={"url_id": 7}).json()

    assert [k["name"] for k in kinds] == ["玄幻", "都市"]
    #  原样声明的串，不是绝对地址
    assert kinds[0]["url"] == "/list/1"


def test_explore_kinds_on_a_source_without_them_is_a_404(client):
    assert client.get("/api/v1/reader/explore/kinds", params={"url_id": 1}).status_code == 404


def test_explore_browses_a_category(explore_client):
    kinds = explore_client.get("/api/v1/reader/explore/kinds", params={"url_id": 7}).json()
    page = explore_client.get(
        "/api/v1/reader/explore", params={"url_id": 7, "url": kinds[0]["url"]}
    ).json()

    assert page["total"] == 1
    assert page["items"][0]["name"] == "剑来"
    #  形状和搜索一致，详情页那条链不必分两种情况
    assert page["items"][0]["sources"] == [
        {"url_id": 7, "source_name": "分类源", "book_url": "https://e.example.com/b/1"}
    ]


def test_explore_on_a_dead_category_is_a_502(explore_client):
    response = explore_client.get("/api/v1/reader/explore", params={"url_id": 7, "url": "/list/2"})
    assert response.status_code == 502


def test_scan_counts_explore_capable_sources(explore_client):
    report = explore_client.post("/api/v1/reader/scan").json()
    assert report["has_explore"] == 1


# ------------------------------------------------------------------ 候选源池


def test_pool_lists_sources_with_a_summary(client):
    """这张表的数据一直存着，在这组端点之前完全没有出口。"""
    client.post("/api/v1/reader/scan")

    page = client.get("/api/v1/pool").json()

    assert page["total"] == 1
    assert page["items"][0]["url_id"] == 1
    assert page["items"][0]["enabled"] is True
    assert page["items"][0]["is_complete"] is True
    assert page["items"][0]["needs_js"] is False
    #  汇总是整个分区的，和当前页无关 —— 界面要一眼看到池子健康度
    assert page["summary"]["total"] == 1
    assert page["summary"]["enabled"] == 1
    #  还没实跑过，所以 proven 是 0。这个数字才是真实可用性
    assert page["summary"]["proven"] == 0


def test_pool_records_proven_sources_after_a_real_run(client):
    client.post("/api/v1/reader/scan")
    client.get("/api/v1/reader/search", params={"keyword": "剑来"})

    page = client.get("/api/v1/pool").json()

    assert page["summary"]["proven"] == 1
    assert page["items"][0]["last_ok_at"]


def test_pool_surfaces_why_a_source_failed(client):
    """在这之前「某个源为什么失败」只存在库里，界面看不到。"""
    client.post("/api/v1/reader/scan")
    client.get("/api/v1/reader/search", params={"keyword": "查无此书"})

    page = client.get("/api/v1/pool", params={"state": "failing"}).json()

    assert page["total"] == 1
    assert page["items"][0]["fail_count"] >= 1
    assert page["items"][0]["last_error"]


def test_pool_filters_by_state_and_keyword(client):
    client.post("/api/v1/reader/scan")

    assert client.get("/api/v1/pool", params={"state": "enabled"}).json()["total"] == 1
    assert client.get("/api/v1/pool", params={"state": "disabled"}).json()["total"] == 0
    assert client.get("/api/v1/pool", params={"q": "甲"}).json()["total"] == 1
    assert client.get("/api/v1/pool", params={"q": "没有"}).json()["total"] == 0


def test_pool_separates_the_two_source_types(client):
    client.post("/api/v1/reader/scan")

    assert client.get("/api/v1/pool", params={"source_type": "book"}).json()["total"] == 1
    assert client.get("/api/v1/pool", params={"source_type": "rss"}).json()["total"] == 0


def test_pool_disable_and_reenable_a_source(client):
    """手动停用一个返回垃圾的源 —— 在这之前做不到。"""
    client.post("/api/v1/reader/scan")

    off = client.patch("/api/v1/pool/book/1", json={"enabled": False}).json()
    assert off["enabled"] is False
    #  停用后搜索不该再用它
    assert client.get("/api/v1/reader/search", params={"keyword": "剑来"}).json()["total"] == 0

    on = client.patch("/api/v1/pool/book/1", json={"enabled": True}).json()
    assert on["enabled"] is True
    assert client.get("/api/v1/reader/search", params={"keyword": "剑来"}).json()["total"] == 1


def test_pool_reset_failures_gives_a_source_a_second_chance(client):
    client.post("/api/v1/reader/scan")
    client.get("/api/v1/reader/search", params={"keyword": "查无此书"})
    assert client.get("/api/v1/pool").json()["items"][0]["fail_count"] >= 1

    body = client.patch("/api/v1/pool/book/1", json={"reset_failures": True}).json()

    assert body["fail_count"] == 0
    assert body["last_error"] == ""


def test_pool_weight_affects_ordering(client):
    client.post("/api/v1/reader/scan")
    body = client.patch("/api/v1/pool/book/1", json={"weight": 50}).json()
    assert body["weight"] == 50


def test_pool_patch_on_an_unknown_source_is_a_404(client):
    assert client.patch("/api/v1/pool/book/99999", json={"enabled": False}).status_code == 404


def test_pool_patch_rejects_an_unknown_source_type(client):
    assert client.patch("/api/v1/pool/video/1", json={"enabled": False}).status_code == 422


def test_pool_batch_toggle_updates_existing_sources_once(client):
    client.post("/api/v1/reader/scan")
    response = client.patch(
        "/api/v1/pool/batch/book",
        json={"url_ids": [1, 1, 999], "enabled": False},
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 1}
    assert client.get("/api/v1/pool").json()["items"][0]["enabled"] is False


def test_pool_latest_test_actions_use_last_result_and_current_type(client):
    client.post("/api/v1/reader/scan")
    db = client.service.database_url
    reader_storage.upsert_source_prefs(
        [
            {"source_type": "book", "url_id": 2, "enabled": False},
            {
                "source_type": "book",
                "url_id": 3,
                "enabled": True,
                "test_count": 3,
                "test_ok_count": 2,
            },
            {"source_type": "rss", "url_id": 4, "enabled": True},
        ],
        database_url=db,
    )
    reader_storage.record_source_test("book", 1, ok=True, database_url=db)
    reader_storage.record_source_test("book", 1, ok=False, error="", database_url=db)
    reader_storage.record_source_test("book", 2, ok=True, database_url=db)
    reader_storage.record_source_test("rss", 4, ok=False, database_url=db)

    summary = client.get("/api/v1/pool", params={"source_type": "book"}).json()["summary"]
    assert summary["last_test_failed_enabled"] == 1
    assert summary["last_test_success_disabled"] == 1
    assert client.patch("/api/v1/pool/batch/book/latest-test/failure").json() == {"updated": 1}
    assert client.patch("/api/v1/pool/batch/book/latest-test/success").json() == {"updated": 1}
    rows = {row["url_id"]: row for row in client.get("/api/v1/pool").json()["items"]}
    assert rows[1]["enabled"] is False and rows[1]["last_test_ok"] is False
    assert rows[2]["enabled"] is True and rows[2]["last_test_ok"] is True
    assert rows[3]["enabled"] is True and rows[3]["last_test_ok"] is None
    client.post("/api/v1/reader/scan")
    assert {row["url_id"]: row["enabled"] for row in client.get("/api/v1/pool").json()["items"]}[
        1
    ] is False
    assert (
        client.get("/api/v1/pool", params={"source_type": "rss"}).json()["items"][0]["enabled"]
        is True
    )
    assert client.patch("/api/v1/pool/batch/book/latest-test/unknown").status_code == 422


def test_pool_filters_untested_and_sorts_last_test_before_pagination(client):
    client.post("/api/v1/reader/scan")
    reader_storage.upsert_source_prefs(
        [
            {
                "source_type": "book",
                "url_id": 1,
                "test_count": 1,
                "last_test_at": datetime(2024, 1, 1),
            },
            {
                "source_type": "book",
                "url_id": 2,
                "test_count": 1,
                "last_test_at": datetime(2025, 1, 1),
            },
            {"source_type": "book", "url_id": 3},
        ],
        database_url=client.service.database_url,
    )

    base = "/api/v1/pool?source_type=book"
    newest = client.get(f"{base}&sort=last_test_desc&limit=1&offset=1").json()
    assert newest["total"] == 3
    assert newest["items"][0]["url_id"] == 1
    oldest = client.get(f"{base}&sort=last_test_asc").json()
    assert [item["url_id"] for item in oldest["items"]] == [1, 2, 3]
    untested = client.get(f"{base}&state=untested").json()
    assert [item["url_id"] for item in untested["items"]] == [3]
    assert untested["items"][0]["last_test_at"] == ""
    assert client.get(f"{base}&limit=1000").json()["limit"] == 1000


def test_pool_exposes_domains_and_sorts_other_columns_before_pagination(client):
    client.post("/api/v1/reader/scan")
    reader_storage.upsert_source_prefs(
        [
            {
                "source_type": "book",
                "url_id": 1,
                "weight": 4,
                "test_count": 2,
                "test_ok_count": 1,
                "source_json": {**SOURCE, "bookSourceUrl": "https://z.example.com/search"},
            },
            {
                "source_type": "book",
                "url_id": 2,
                "name": "乙源",
                "weight": 9,
                "test_count": 2,
                "test_ok_count": 2,
                "source_json": {**SOURCE, "bookSourceUrl": "https://a.example.com/search"},
            },
            {"source_type": "book", "url_id": 3, "name": "丙源", "weight": 1},
        ],
        database_url=client.service.database_url,
    )
    upsert_source_detail_record(
        "https://z.example.com/search",
        "book",
        source_id=1,
        database_url=client.service.database_url,
    )
    upsert_source_detail_record(
        "https://a.example.com/search",
        "book",
        source_id=2,
        database_url=client.service.database_url,
    )
    base = "/api/v1/pool?source_type=book"
    assert client.get(f"{base}&sort=weight_desc&limit=1&offset=1").json()["items"][0]["url_id"] == 1
    assert client.get(f"{base}&sort=success_rate_desc").json()["items"][-1]["url_id"] == 3
    first = client.get(f"{base}&sort=domain_asc&limit=1").json()["items"][0]
    assert first["url_id"] == 2
    assert first["domain"] == "a.example.com"
    assert first["source_url"] == "https://a.example.com/search"
    assert (
        client.patch("/api/v1/pool/book/2", json={"weight": 8}).json()["domain"] == "a.example.com"
    )


def test_pool_rules_can_be_inspected_and_edited_without_stale_cache(client):
    client.post("/api/v1/reader/scan")
    path = "/api/v1/pool/book/1/rules"
    detail = client.get(path).json()
    assert detail["rules"]["bookSourceUrl"] == SOURCE["bookSourceUrl"]
    assert detail["source"]["source_url"] == SOURCE["bookSourceUrl"]
    assert client.service.registry.load_spec(1).name == SOURCE["bookSourceName"]

    rules = {**detail["rules"], "bookSourceName": "改名源", "exploreUrl": "分类::/c"}
    saved = client.put(path, json={"rules": rules, "updated_at": detail["updated_at"]})
    assert saved.status_code == 200
    assert saved.json()["source"]["name"] == "改名源"
    assert saved.json()["source"]["has_explore"] is True
    assert client.service.registry.load_spec(1).name == "改名源"
    assert client.get("/api/v1/pool").json()["items"][0]["name"] == "改名源"
    assert (
        client.put(path, json={"rules": rules, "updated_at": detail["updated_at"]}).status_code
        == 409
    )
    assert (
        client.put(path, json={"rules": [], "updated_at": saved.json()["updated_at"]}).status_code
        == 422
    )


def test_pool_rule_address_edit_updates_the_existing_index(client):
    client.post("/api/v1/reader/scan")
    upsert_source_detail_record(
        "a.example.com", "book", source_id=1, database_url=client.service.database_url
    )
    path = "/api/v1/pool/book/1/rules"
    detail = client.get(path).json()
    changed = {**detail["rules"], "bookSourceUrl": "https://fixed.example.org/path"}
    saved = client.put(path, json={"rules": changed, "updated_at": detail["updated_at"]})
    assert saved.status_code == 200
    assert saved.json()["index_key"] == "fixed.example.org"
    assert saved.json()["source"]["source_url"] == "https://fixed.example.org/path"
    assert client.get("/api/v1/pool").json()["items"][0]["domain"] == "fixed.example.org"
    assert (
        client.put(
            path,
            json={
                "rules": {**changed, "bookSourceUrl": "oops"},
                "updated_at": saved.json()["updated_at"],
            },
        ).status_code
        == 422
    )


def test_pool_clear_queue_cancels_waiting_and_stops_active_after_current_source(
    client, monkeypatch
):
    from funread_api.v1 import pool

    client.post("/api/v1/reader/scan")
    reader_storage.upsert_source_prefs(
        [{"source_type": "book", "url_id": 2}], database_url=client.service.database_url
    )
    started = threading.Event()
    release = threading.Event()
    tested = []

    def test_source(url_id):
        tested.append(url_id)
        started.set()
        assert release.wait(5)
        return True, ""

    monkeypatch.setattr(client.service, "test_source", test_source)
    try:
        first = client.post(
            "/api/v1/pool/test", json={"source_type": "book", "url_ids": [1, 2]}
        ).json()
        assert started.wait(5)
        second = client.post(
            "/api/v1/pool/test", json={"source_type": "book", "url_ids": [2]}
        ).json()
        assert client.delete("/api/v1/pool/test/queue").json() == {"cancelled": 2}
        assert client.get(f"/api/v1/pool/test/{first['task_id']}").json()["state"] == "cancelled"
        assert client.get(f"/api/v1/pool/test/{second['task_id']}").json()["state"] == "cancelled"
        release.set()
        worker = pool._source_test_queue._worker
        assert worker is not None
        worker.join(timeout=5)
        assert tested == [1]
        assert pool._source_test_queue._pending == {}
    finally:
        release.set()


def test_pool_one_click_tests_all_enabled_sources_oldest_first(client, monkeypatch):
    from funread_api.v1 import pool

    client.post("/api/v1/reader/scan")
    reader_storage.upsert_source_prefs(
        [
            {
                "source_type": "book",
                "url_id": 1,
                "enabled": True,
                "last_test_at": datetime(2025, 1, 1),
                "source_json": SOURCE,
            },
            {
                "source_type": "book",
                "url_id": 2,
                "enabled": True,
                "last_test_at": datetime(2024, 1, 1),
                "source_json": SOURCE,
            },
            {"source_type": "book", "url_id": 3, "enabled": True, "source_json": SOURCE},
            {"source_type": "book", "url_id": 4, "enabled": False, "source_json": SOURCE},
            {"source_type": "book", "url_id": 5, "enabled": True},
        ],
        database_url=client.service.database_url,
    )
    started = threading.Event()
    captured = []

    def run(task_id, source_type, url_ids, database_url):
        captured.extend(url_ids)
        pool.get_source_test_tracker().finish(task_id, done=len(url_ids), failed=0)
        started.set()

    monkeypatch.setattr(pool, "_run_source_test", run)
    response = client.post("/api/v1/pool/test/all?source_type=book")
    assert response.status_code == 202
    assert response.json()["queued"] == 4
    assert started.wait(5)
    assert captured == [3, 4, 2, 1]


def test_pool_test_queue_spaces_repeated_domains(monkeypatch):
    from types import SimpleNamespace

    from funread_api.v1 import pool

    queue = pool.SourceTestQueue()
    clock = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(pool, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    assert queue.wait_for_domain("task", "same.example")
    assert queue.wait_for_domain("task", "other.example")
    assert sleeps == []
    assert queue.wait_for_domain("task", "same.example")
    assert sum(sleeps) == 30
    queue._cancelled.add("task")
    assert not queue.wait_for_domain("task", "same.example")


def test_pool_rss_ai_optimizer_suggests_only_rules_that_parse_the_fetched_page(client, monkeypatch):
    from funread_api.v1 import rss_optimize
    from funread_api.v1.deps import get_rss_service

    rules = {
        "sourceUrl": "https://rss.example.com/list",
        "sourceName": "RSS 源",
        "ruleArticles": "class.missing",
        "ruleTitle": "tag.h2@text",
    }
    reader_storage.upsert_source_prefs(
        [{"source_type": "rss", "url_id": 7, "source_json": rules}],
        database_url=client.service.database_url,
    )
    get_rss_service().fetcher_factory = lambda timeout=None: StaticFetcher(
        {
            "https://rss.example.com/list": '<div class="item"><h2>文章一</h2><a href="/one">阅读全文</a></div>'
        }
    )
    monkeypatch.setattr(
        rss_optimize,
        "read_secret",
        lambda *args: {
            "base_url": "https://model.example.com/v1",
            "model": "deepseek/deepseek-chat",
            "api_key": "test-key",
        }[args[-1]],
    )
    sent = []
    proposed = {"articles": "class.item", "title": "tag.h2@text", "link": "tag.a@href"}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "arguments": json.dumps(
                                            {
                                                "reason": "列表选择器已变更",
                                                "rules": proposed,
                                            }
                                        )
                                    }
                                }
                            ]
                        }
                    }
                ]
            }

    def post(url, **kwargs):
        sent.append((url, kwargs))
        return Response()

    monkeypatch.setattr(rss_optimize.requests, "post", post)
    result = client.post("/api/v1/pool/rss/7/optimize")
    assert result.status_code == 200
    assert result.json()["status"] == "suggested"
    assert result.json()["after_count"] == 1
    assert result.json()["rules"]["ruleRss"]["articles"] == "class.item"
    assert sent[0][0] == "https://model.example.com/v1/chat/completions"
    assert sent[0][1]["json"]["tools"][0]["function"]["name"] == "suggest_rss_rules"
    assert client.get("/api/v1/pool/rss/7/rules").json()["rules"] == rules

    working = {**rules, "ruleRss": proposed}
    reader_storage.upsert_source_prefs(
        [{"source_type": "rss", "url_id": 7, "source_json": working}],
        database_url=client.service.database_url,
    )
    assert client.post("/api/v1/pool/rss/7/optimize").json()["status"] == "ok"
    assert len(sent) == 1
    reader_storage.upsert_source_prefs(
        [{"source_type": "rss", "url_id": 7, "source_json": rules}],
        database_url=client.service.database_url,
    )
    proposed["articles"] = "class.nope"
    assert client.post("/api/v1/pool/rss/7/optimize").status_code == 422


def test_pool_tests_only_selected_sources_even_when_disabled(client):
    client.post("/api/v1/reader/scan")
    client.patch("/api/v1/pool/book/1", json={"enabled": False})

    response = client.post(
        "/api/v1/pool/test",
        json={"source_type": "book", "url_ids": [1, 1, 999]},
    )

    assert response.status_code == 202
    assert response.json()["queued"] == 1
    task_id = response.json()["task_id"]
    deadline = time.monotonic() + 5
    while client.get(f"/api/v1/pool/test/{task_id}").json()["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    row = client.get("/api/v1/pool", params={"source_type": "book"}).json()["items"][0]
    assert row["test_count"] == 1


def test_pool_accepts_another_batch_while_the_consumer_is_busy(client, monkeypatch):
    from funread_api.v1 import pool

    client.post("/api/v1/reader/scan")
    first_started = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    order = []

    def run(task_id, source_type, url_ids, database_url):
        order.append(task_id)
        if len(order) == 1:
            first_started.set()
            assert release_first.wait(5)
        else:
            second_done.set()
        pool.get_source_test_tracker().finish(task_id, done=len(url_ids), failed=0)

    monkeypatch.setattr(pool, "_run_source_test", run)
    try:
        first = client.post("/api/v1/pool/test", json={"source_type": "book", "url_ids": [1]})
        assert first.status_code == 202
        assert first_started.wait(5)
        second = client.post("/api/v1/pool/test", json={"source_type": "book", "url_ids": [1]})
        assert second.status_code == 202
        assert not second_done.is_set()
        release_first.set()
        assert second_done.wait(5)
        worker = pool._source_test_queue._worker
        if worker is not None:
            worker.join(timeout=5)
        assert pool._source_test_queue._worker is None
        assert order == [first.json()["task_id"], second.json()["task_id"]]
    finally:
        release_first.set()


def test_pool_auto_batches_skip_sources_already_in_the_queue(monkeypatch):
    from funread_api.v1 import pool

    queue = pool.SourceTestQueue()
    first_started = threading.Event()
    release_first = threading.Event()
    tested = []

    def run(task_id, source_type, url_ids, database_url):
        tested.extend(url_ids)
        if len(tested) == 1:
            first_started.set()
            assert release_first.wait(5)

    monkeypatch.setattr(pool, "_run_source_test", run)
    try:
        first = queue.submit("book", [1, 2], 1, False, 1, None)
        assert first is not None
        assert first_started.wait(5)
        second = queue.submit("book", [1, 2], 1, False, 1, None)
        assert second is not None
        release_first.set()
        worker = queue._worker
        assert worker is not None
        worker.join(timeout=5)
        assert queue._worker is None
        assert tested == [1, 2]
        third = queue.submit("book", [1], 1, False, 1, None)
        assert third is not None
        restarted = queue._worker
        assert restarted is not None and restarted is not worker
        restarted.join(timeout=5)
        assert queue._worker is None
        assert tested == [1, 2, 1]
    finally:
        release_first.set()


def test_pool_is_admin_only(client):
    """改一个源的启停会影响所有用户的搜索结果，不该由任意读者改。"""
    from accounts_support import make_account

    config.set_overrides(api_password="hunter2")
    make_account("alice", "password123")

    assert client.get("/api/v1/pool").status_code == 401
    assert client.patch("/api/v1/pool/book/1", json={"enabled": False}).status_code == 401
    assert client.delete("/api/v1/pool/test/queue").status_code == 401
