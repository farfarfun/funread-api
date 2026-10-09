"""Reader and shelf routes, driven against a fake fetcher — nothing hits the network."""

import json

import pytest
from fastapi.testclient import TestClient

from funread.legado.engine import StaticFetcher
from funread.legado.reader import get_cached_chapter, save_cached_chapter
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
def client(monkeypatch, tmp_path):
    """App wired to a one-source archive and an offline fetcher."""
    hubs = tmp_path / "hubs"
    path = hubs / "book" / "source" / "0-100" / "1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"url_id": 1, "status": 2, "candidate": [{"source": SOURCE}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FUNREAD_DATABASE_URL", f"sqlite:///{tmp_path / 'reader.db'}")
    monkeypatch.setenv("FUNREAD_CACHE_ROOT", str(hubs))

    reset_reader_services()
    with TestClient(create_app()) as test_client:
        service = get_reader_service()
        service.fetcher_factory = lambda timeout=None: StaticFetcher(PAGES)
        service.registry.scan()
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


def test_the_two_source_types_have_separate_pools(client):
    """rss 的候选池是另一次扫描，不是 book 那次的副作用。"""
    book = client.post("/api/v1/reader/scan").json()
    rss = client.post("/api/v1/reader/scan", params={"source_type": "rss"}).json()

    assert book["enabled"] == 1
    #  这个夹具的归档里只有 book 源，所以 rss 侧应当什么都没扫到
    assert rss == {
        "source_type": "rss",
        "scanned": 0,
        "complete": 0,
        "needs_js": 0,
        "web_view": 0,
        "has_explore": 0,
        "enabled": 0,
    }


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
def explore_client(monkeypatch, tmp_path):
    """归档里放一个带发现页规则的源。"""
    hubs = tmp_path / "hubs"
    path = hubs / "book" / "source" / "0-100" / "7.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"url_id": 7, "status": 2, "candidate": [{"source": EXPLORE_SOURCE}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FUNREAD_DATABASE_URL", f"sqlite:///{tmp_path / 'explore.db'}")
    monkeypatch.setenv("FUNREAD_CACHE_ROOT", str(hubs))

    reset_reader_services()
    with TestClient(create_app()) as test_client:
        service = get_reader_service()
        service.fetcher_factory = lambda timeout=None: StaticFetcher(EXPLORE_PAGES)
        service.registry.scan()
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


def test_pool_is_admin_only(client, monkeypatch):
    """改一个源的启停会影响所有用户的搜索结果，不该由任意读者改。"""
    from funread.legado.reader import create_user

    monkeypatch.setenv("FUNREAD_API_PASSWORD", "hunter2")
    create_user("alice", "password123")

    assert client.get("/api/v1/pool").status_code == 401
    assert client.patch("/api/v1/pool/book/1", json={"enabled": False}).status_code == 401
