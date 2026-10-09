"""Subscription routes, driven against a fake fetcher — nothing hits the network."""

import json

import pytest
from fastapi.testclient import TestClient

from funread.legado.engine import StaticFetcher
from funread.legado.reader import create_user
from funread_api.app import create_app
from funread_api.v1.deps import get_rss_service, reset_reader_services

SOURCE_BASE = "https://feed.example.com"
FEED_URL = "https://blog.example.com/feed.xml"

LEGADO_SOURCE = {
    "sourceUrl": SOURCE_BASE,
    "sourceName": "归档订阅源",
    "sourceGroup": "科技",
    "sourceIcon": f"{SOURCE_BASE}/icon.png",
    "sortUrl": f"头条::{SOURCE_BASE}\n科技::{SOURCE_BASE}/tech",
    "ruleArticles": "class.item",
    "ruleTitle": "tag.h2@text",
    "ruleLink": "tag.a@href",
    "ruleContent": "class.body@html",
    "ruleNextPage": "class.next@href",
}

WEB_VIEW_SOURCE = {**LEGADO_SOURCE, "singleUrl": f"{SOURCE_BASE}/p", "sourceName": "WebView 源"}

LIST_HTML = (
    '<div class="list">'
    '<div class="item"><h2><a href="/a/1">归档第一篇</a></h2></div>'
    '<div class="item"><h2><a href="/a/2">归档第二篇</a></h2></div>'
    "</div>"
    '<a class="next" href="/list?p=2">下一页</a>'
)

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>示例博客</title>
  <item><title>第一篇</title><link>https://blog.example.com/1</link>
        <description>摘要一</description></item>
  <item><title>第二篇</title><link>https://blog.example.com/2</link>
        <description>摘要二</description></item>
</channel></rss>
"""

PAGES = {
    FEED_URL: RSS_XML,
    "https://blog.example.com/not-a-feed": "<html><body><h1>首页</h1></body></html>",
    SOURCE_BASE: LIST_HTML,
    f"{SOURCE_BASE}/tech": LIST_HTML,
    f"{SOURCE_BASE}/list?p=2": '<div class="list"></div>',
    f"{SOURCE_BASE}/a/1": '<div class="body"><p>归档正文</p></div>',
}


def _write_source(hubs, url_id, payload):
    bucket = (url_id // 100) * 100
    path = hubs / "rss" / "source" / f"{bucket}-{bucket + 100}" / f"{url_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def client(monkeypatch, tmp_path):
    """App wired to a two-source rss archive and an offline fetcher."""
    hubs = tmp_path / "hubs"
    _write_source(hubs, 1, LEGADO_SOURCE)
    _write_source(hubs, 2, WEB_VIEW_SOURCE)
    monkeypatch.setenv("FUNREAD_DATABASE_URL", f"sqlite:///{tmp_path / 'rss.db'}")
    monkeypatch.setenv("FUNREAD_CACHE_ROOT", str(hubs))

    reset_reader_services()
    with TestClient(create_app()) as test_client:
        service = get_rss_service()
        service.fetcher_factory = lambda timeout=None: StaticFetcher(dict(PAGES))
        #  The pool starts empty, same as a real install before the first scan.
        test_client.post("/api/v1/reader/scan", params={"source_type": "rss"})
        yield test_client
    reset_reader_services()


def _subscribe_feed(client, url=FEED_URL, **extra):
    return client.post("/api/v1/rss/subscriptions", json={"kind": "feed", "feed_url": url, **extra})


def _subscribe_legado(client, url_id=1, **extra):
    return client.post(
        "/api/v1/rss/subscriptions", json={"kind": "legado", "url_id": url_id, **extra}
    )


# ---------------------------------------------------------------- 源目录


def test_source_directory_lists_only_runnable_sources(client):
    page = client.get("/api/v1/rss/sources").json()

    assert page["total"] == 1
    assert page["items"][0]["name"] == "归档订阅源"
    assert page["items"][0]["group"] == "科技"
    assert page["items"][0]["categories"] == ["头条", "科技"]


def test_source_directory_is_empty_before_the_first_scan(monkeypatch, tmp_path):
    """真实安装在首次扫描前就是空的，界面得能分辨这个状态。"""
    hubs = tmp_path / "hubs"
    _write_source(hubs, 1, LEGADO_SOURCE)
    monkeypatch.setenv("FUNREAD_DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setenv("FUNREAD_CACHE_ROOT", str(hubs))
    reset_reader_services()
    with TestClient(create_app()) as fresh:
        assert fresh.get("/api/v1/rss/sources").json()["total"] == 0
    reset_reader_services()


def test_source_directory_filters_and_paginates(client):
    assert client.get("/api/v1/rss/sources", params={"q": "归档"}).json()["total"] == 1
    assert client.get("/api/v1/rss/sources", params={"q": "没有"}).json()["total"] == 0
    page = client.get("/api/v1/rss/sources", params={"limit": 1, "offset": 9}).json()
    assert page["items"] == []
    assert page["total"] == 1


# ---------------------------------------------------------------- 订阅


def test_subscribe_to_a_feed(client):
    response = _subscribe_feed(client)

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "feed"
    assert body["title"] == "示例博客"
    assert body["preview"] == 2
    assert body["sub_id"]


def test_subscribe_to_an_archived_source(client):
    response = _subscribe_legado(client)

    assert response.status_code == 201
    assert response.json()["title"] == "归档订阅源"
    assert response.json()["icon"] == f"{SOURCE_BASE}/icon.png"


def test_subscribing_twice_is_idempotent(client):
    first = _subscribe_feed(client).json()["sub_id"]
    second = _subscribe_feed(client, title="改名").json()["sub_id"]

    assert first == second
    subscriptions = client.get("/api/v1/rss/subscriptions").json()
    assert len(subscriptions) == 1
    assert subscriptions[0]["title"] == "改名"


def test_a_bad_feed_url_is_a_400_with_the_reason(client):
    """贴了网页首页当 feed 是最常见的误用。"""
    response = _subscribe_feed(client, url="https://blog.example.com/not-a-feed")

    assert response.status_code == 400
    assert "HTML 页面" in response.json()["detail"]
    assert client.get("/api/v1/rss/subscriptions").json() == []


def test_a_non_http_feed_url_is_rejected(client):
    for bad in ("file:///etc/passwd", "ftp://x/f", "javascript:alert(1)"):
        response = _subscribe_feed(client, url=bad)
        assert response.status_code == 400, bad


def test_a_web_view_source_cannot_be_subscribed(client):
    """它不在候选池里，所以这里是 404 而不是 422。"""
    assert _subscribe_legado(client, url_id=2).status_code in (404, 422)
    assert client.get("/api/v1/rss/subscriptions").json() == []


def test_subscribe_validates_the_kind_specific_field(client):
    assert client.post("/api/v1/rss/subscriptions", json={"kind": "legado"}).status_code == 422
    assert (
        client.post("/api/v1/rss/subscriptions", json={"kind": "feed", "feed_url": " "}).status_code
        == 422
    )
    assert (
        client.post("/api/v1/rss/subscriptions", json={"kind": "video", "url_id": 1}).status_code
        == 422
    )


def test_unsubscribe(client):
    sub_id = _subscribe_feed(client).json()["sub_id"]

    assert client.delete(f"/api/v1/rss/subscriptions/{sub_id}").status_code == 204
    assert client.get("/api/v1/rss/subscriptions").json() == []


def test_unsubscribing_something_that_is_not_there_is_a_404(client):
    assert client.delete("/api/v1/rss/subscriptions/nope").status_code == 404


# ---------------------------------------------------------------- 分类


def test_categories_of_a_legado_subscription(client):
    sub_id = _subscribe_legado(client).json()["sub_id"]
    names = [
        item["name"] for item in client.get(f"/api/v1/rss/subscriptions/{sub_id}/categories").json()
    ]
    assert names == ["头条", "科技"]


def test_categories_of_a_feed_subscription(client):
    sub_id = _subscribe_feed(client).json()["sub_id"]
    assert len(client.get(f"/api/v1/rss/subscriptions/{sub_id}/categories").json()) == 1


# ---------------------------------------------------------------- 列表


def test_articles_from_a_feed(client):
    sub_id = _subscribe_feed(client).json()["sub_id"]
    page = client.get("/api/v1/rss/articles", params={"sub_id": sub_id}).json()

    assert [a["title"] for a in page["items"]] == ["第一篇", "第二篇"]
    assert page["items"][0]["read"] is False
    assert page["next_url"] == ""
    assert page["has_content"] is True


def test_articles_from_an_archived_source_carry_a_next_url(client):
    sub_id = _subscribe_legado(client).json()["sub_id"]
    page = client.get("/api/v1/rss/articles", params={"sub_id": sub_id}).json()

    assert [a["title"] for a in page["items"]] == ["归档第一篇", "归档第二篇"]
    assert page["next_url"] == f"{SOURCE_BASE}/list?p=2"


def test_next_url_fetches_the_following_page(client):
    sub_id = _subscribe_legado(client).json()["sub_id"]
    page = client.get(
        "/api/v1/rss/articles",
        params={"sub_id": sub_id, "next_url": f"{SOURCE_BASE}/list?p=2"},
    ).json()
    assert page["items"] == []


def test_articles_respects_the_limit(client):
    sub_id = _subscribe_feed(client).json()["sub_id"]
    page = client.get("/api/v1/rss/articles", params={"sub_id": sub_id, "limit": 1}).json()
    assert len(page["items"]) == 1


def test_articles_of_an_unknown_subscription_is_a_404(client):
    assert client.get("/api/v1/rss/articles", params={"sub_id": "nope"}).status_code == 404


def test_a_fetch_failure_shows_up_as_last_error(client):
    """界面要能解释「为什么这个订阅是空的」。"""
    sub_id = _subscribe_feed(client).json()["sub_id"]
    get_rss_service().fetcher_factory = lambda timeout=None: StaticFetcher({})

    assert client.get("/api/v1/rss/articles", params={"sub_id": sub_id}).status_code == 502
    assert client.get("/api/v1/rss/subscriptions").json()[0]["last_error"]


# ---------------------------------------------------------------- 正文


def test_article_text_from_an_archived_source(client):
    sub_id = _subscribe_legado(client).json()["sub_id"]
    body = client.get(
        "/api/v1/rss/article", params={"sub_id": sub_id, "link": f"{SOURCE_BASE}/a/1"}
    ).json()

    assert "归档正文" in body["content_html"]
    assert body["article_key"]


def test_article_text_from_a_feed(client):
    sub_id = _subscribe_feed(client).json()["sub_id"]
    body = client.get(
        "/api/v1/rss/article",
        params={"sub_id": sub_id, "link": "https://blog.example.com/1"},
    ).json()

    assert body["title"] == "第一篇"
    assert "摘要一" in body["content_html"]


def test_an_article_that_left_the_feed_is_a_404(client):
    sub_id = _subscribe_feed(client).json()["sub_id"]
    response = client.get(
        "/api/v1/rss/article",
        params={"sub_id": sub_id, "link": "https://blog.example.com/999"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------- 状态


def test_mark_read_round_trips(client):
    sub_id = _subscribe_feed(client).json()["sub_id"]
    key = client.get("/api/v1/rss/articles", params={"sub_id": sub_id}).json()["items"][0][
        "article_key"
    ]

    marked = client.put(
        f"/api/v1/rss/subscriptions/{sub_id}/articles/{key}/read", json={"read": True}
    )
    assert marked.status_code == 204

    page = client.get("/api/v1/rss/articles", params={"sub_id": sub_id}).json()
    assert page["items"][0]["read"] is True
    assert client.get("/api/v1/rss/subscriptions").json()[0]["read_count"] == 1


def test_mark_unread_again(client):
    sub_id = _subscribe_feed(client).json()["sub_id"]
    key = client.get("/api/v1/rss/articles", params={"sub_id": sub_id}).json()["items"][0][
        "article_key"
    ]
    client.put(f"/api/v1/rss/subscriptions/{sub_id}/articles/{key}/read", json={"read": True})
    client.put(f"/api/v1/rss/subscriptions/{sub_id}/articles/{key}/read", json={"read": False})

    assert client.get("/api/v1/rss/subscriptions").json()[0]["read_count"] == 0


def test_favorite_survives_the_article_leaving_the_feed(client):
    """收藏要能脱离原始列表渲染 —— 所以标题随请求存一份。"""
    sub_id = _subscribe_feed(client).json()["sub_id"]
    item = client.get("/api/v1/rss/articles", params={"sub_id": sub_id}).json()["items"][0]

    client.put(
        f"/api/v1/rss/subscriptions/{sub_id}/articles/{item['article_key']}/favorite",
        json={"favorited": True, "title": item["title"], "link": item["link"]},
    )

    favorites = client.get("/api/v1/rss/favorites").json()
    assert [row["title"] for row in favorites] == ["第一篇"]


def test_read_all(client):
    sub_id = _subscribe_feed(client).json()["sub_id"]
    keys = [
        a["article_key"]
        for a in client.get("/api/v1/rss/articles", params={"sub_id": sub_id}).json()["items"]
    ]

    response = client.put(
        f"/api/v1/rss/subscriptions/{sub_id}/read-all", json={"article_keys": keys}
    )
    assert response.status_code == 200
    assert response.json()["changed"] == 2
    assert client.get("/api/v1/rss/subscriptions").json()[0]["read_count"] == 2


def test_read_all_needs_at_least_one_key(client):
    sub_id = _subscribe_feed(client).json()["sub_id"]
    response = client.put(f"/api/v1/rss/subscriptions/{sub_id}/read-all", json={"article_keys": []})
    assert response.status_code == 422


def test_state_writes_are_scoped_to_the_subscription(client):
    """不能拿别人的 sub_id 去写状态。"""
    response = client.put("/api/v1/rss/subscriptions/nope/articles/abc/read", json={"read": True})
    assert response.status_code == 404


# ---------------------------------------------------------------- 鉴权


def test_subscriptions_do_not_leak_between_accounts(client, monkeypatch):
    monkeypatch.setenv("FUNREAD_REGISTER_CODE", "letmein")
    alice = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "password": "password123", "code": "letmein"},
    )
    assert alice.status_code == 201
    sub_id = _subscribe_feed(client).json()["sub_id"]

    client.post("/api/v1/auth/logout")
    client.post(
        "/api/v1/auth/register",
        json={"username": "bob", "password": "password123", "code": "letmein"},
    )

    assert client.get("/api/v1/rss/subscriptions").json() == []
    #  Knowing the id is not access
    assert client.delete(f"/api/v1/rss/subscriptions/{sub_id}").status_code == 404
    assert client.get("/api/v1/rss/articles", params={"sub_id": sub_id}).status_code == 404


def test_rss_needs_an_identity_once_accounts_exist(client):
    create_user("alice", "password123")
    assert client.get("/api/v1/rss/subscriptions").status_code == 401
    assert _subscribe_feed(client).status_code == 401


def test_reader_public_does_not_open_the_ssrf_endpoint(client, monkeypatch):
    """POST /rss/subscriptions 让服务端拉任意 URL；它不随「公开阅读端」放开。"""
    monkeypatch.setenv("FUNREAD_API_PASSWORD", "hunter2")
    monkeypatch.setenv("FUNREAD_READER_PUBLIC", "1")
    create_user("alice", "password123")

    assert _subscribe_feed(client).status_code == 401
    assert client.get("/api/v1/rss/sources").status_code == 401


# ---------------------------------------------------------------- variables 回传


def test_article_variables_round_trip(client, monkeypatch):
    """列表页 @put、正文页 @get 的源要靠这个。丢掉就静默读到空正文。"""
    seen: dict = {}
    service = get_rss_service()
    original = service.article

    def spy(sub_id, user_id, link, variables=None, title=""):
        seen["variables"] = variables
        return original(sub_id, user_id, link, variables=variables, title=title)

    monkeypatch.setattr(service, "article", spy)
    sub_id = _subscribe_feed(client).json()["sub_id"]

    response = client.get(
        "/api/v1/rss/article",
        params={
            "sub_id": sub_id,
            "link": "https://blog.example.com/1",
            "variables": '{"token":"abc","page":"2"}',
        },
    )

    assert response.status_code == 200
    assert seen["variables"] == {"token": "abc", "page": "2"}


def test_article_without_variables_passes_an_empty_dict(client, monkeypatch):
    seen: dict = {}
    service = get_rss_service()
    original = service.article
    monkeypatch.setattr(
        service,
        "article",
        lambda *a, **kw: seen.update(kw) or original(*a, **kw),
    )
    sub_id = _subscribe_feed(client).json()["sub_id"]

    client.get(
        "/api/v1/rss/article",
        params={"sub_id": sub_id, "link": "https://blog.example.com/1"},
    )

    assert seen["variables"] == {}


@pytest.mark.parametrize("raw", ["{not json", "[1,2]", '"a string"', "42"])
def test_malformed_variables_is_a_422_not_a_silent_drop(client, raw):
    """静默丢掉 variables 正是这个参数要防的那种失败。"""
    sub_id = _subscribe_feed(client).json()["sub_id"]

    response = client.get(
        "/api/v1/rss/article",
        params={"sub_id": sub_id, "link": "https://blog.example.com/1", "variables": raw},
    )

    assert response.status_code == 422
    assert "variables" in response.json()["detail"]


def test_list_response_carries_variables(client):
    """列表给出 variables，正文页才有东西可回传 —— 两头必须都在。"""
    sub_id = _subscribe_feed(client).json()["sub_id"]
    page = client.get("/api/v1/rss/articles", params={"sub_id": sub_id}).json()

    assert "variables" in page["items"][0]
    assert isinstance(page["items"][0]["variables"], dict)
