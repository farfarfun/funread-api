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

    assert report == {"scanned": 1, "complete": 1, "needs_js": 0, "enabled": 1}


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
    assert response.json() == {"items": [], "total": 0}


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
    assert accepted.json() == {"book_key": book_key, "queued": 2}
    #  TestClient drains background tasks before returning, so the work is done
    assert client.get(f"/api/v1/shelf/{book_key}/cached").json()["chapter_indexes"] == [1, 2]
    assert client.delete(f"/api/v1/shelf/{book_key}/cached").json()["chapter_indexes"] == []


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

    assert response.status_code == 204
    assert get_cached_chapter(book_key, 1, database_url=client.service.database_url) is None
