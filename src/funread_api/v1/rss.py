"""Subscriptions: source directory, subscribe, article list, article text, state.

Two kinds of subscription behind one set of endpoints -- an archived Legado RSS
source (``kind="legado"``) or a feed URL the user typed (``kind="feed"``). The
front-end sees one list either way; which parser runs is the service's business.

**``POST /subscriptions`` with ``kind="feed"`` makes the server fetch an
arbitrary URL -- an SSRF primitive.** It therefore sits behind
``require_user``, which means: never reachable unauthenticated, and never
opened by ``FUNREAD_READER_PUBLIC`` (that flag is about *reading*). Reader
accounts are invite-gated (``FUNREAD_REGISTER_CODE``), so "authenticated" here
means "someone the operator let in". Private-range targets are *not* blocked:
on a self-hosted LAN box, subscribing to another machine on the same network is
a legitimate thing to want, and blocking it would break real use.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from funread.legado.reader import KIND_FEED, KIND_LEGADO
from funread_api.security import CurrentUser, require_user

from .deps import engine_errors, get_rss_service

router = APIRouter(prefix="/rss", tags=["rss"])

#: Cap on one article-list page. The upstream page size is whatever the source
#: gives; this only bounds what we hand back.
MAX_ARTICLES_PER_PAGE = 100

#: Cap on one "mark all read" batch. It is a list of keys from one visible
#: page, so a few hundred is already generous.
MAX_READ_ALL_KEYS = 500


class RssSourceOut(BaseModel):
    url_id: int
    name: str
    group: str = ""
    icon: str = ""
    needs_js: bool = False
    categories: list[str] = Field(default_factory=list)


class RssSourcePage(BaseModel):
    items: list[RssSourceOut]
    total: int
    limit: int
    offset: int


class SubscribeIn(BaseModel):
    kind: str = Field(pattern=f"^({KIND_LEGADO}|{KIND_FEED})$")
    #: Required for kind="legado".
    url_id: int | None = None
    #: Required for kind="feed".
    feed_url: str = ""
    title: str = ""


class SubscriptionOut(BaseModel):
    sub_id: str
    kind: str
    url_id: int
    feed_url: str
    title: str
    icon: str
    group: str
    last_fetched_at: str
    #: Why the last refresh came back empty, if it did. The UI needs this to
    #: avoid presenting a failure as "nothing new".
    last_error: str
    read_count: int
    #: Only set right after subscribing to a feed: how many items it had.
    preview: int | None = None


class CategoryOut(BaseModel):
    name: str
    url: str


class ArticleOut(BaseModel):
    article_key: str
    title: str
    link: str
    pub_date: str
    description: str
    image: str
    read: bool
    favorited: bool
    #: Round-tripped back into GET /article -- rules capture values with @put
    #: on the list page and read them with @get on the article page.
    variables: dict[str, str] = Field(default_factory=dict)


class ArticlePage(BaseModel):
    items: list[ArticleOut]
    #: Pass back as ``next_url`` to get the following page. Empty = no more.
    next_url: str
    category: str
    #: False when the source has no content rule: the UI should open the link
    #: externally instead of offering an in-app reader that would be blank.
    has_content: bool


class ArticleDetail(BaseModel):
    article_key: str
    title: str
    link: str
    pub_date: str
    description: str
    image: str
    content_html: str


class ReadIn(BaseModel):
    read: bool = True
    #: Optional snapshot so the favourites list can render without the feed.
    title: str = ""
    link: str = ""
    pub_date: str = ""
    image: str = ""


class FavoriteIn(BaseModel):
    favorited: bool = True
    title: str = ""
    link: str = ""
    pub_date: str = ""
    image: str = ""


class ReadAllIn(BaseModel):
    article_keys: list[str] = Field(min_length=1, max_length=MAX_READ_ALL_KEYS)


class ReadAllOut(BaseModel):
    changed: int


class FavoriteOut(BaseModel):
    sub_id: str
    article_key: str
    title: str
    link: str
    pub_date: str
    image: str
    read: bool


def _parse_variables(raw: str) -> dict[str, str]:
    """Decode the JSON-encoded ``variables`` query parameter.

    Malformed input is the caller's bug, so it is a 422 rather than a silent
    empty dict -- silently dropping variables is exactly the failure this
    parameter exists to prevent.
    """
    if not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"variables 不是合法的 JSON：{error}",
        ) from error
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="variables 必须是一个 JSON 对象",
        )
    return {str(key): str(value) for key, value in decoded.items()}


def _meta(payload: ReadIn | FavoriteIn) -> dict[str, str]:
    return {
        "title": payload.title,
        "link": payload.link,
        "pub_date": payload.pub_date,
        "image": payload.image,
    }


# ---------------------------------------------------------------------------
# Source directory
# ---------------------------------------------------------------------------


@router.get("/sources", response_model=RssSourcePage)
def list_sources(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(require_user),
) -> RssSourcePage:
    """Archived Legado subscription sources that this build can actually run.

    Only ``enabled`` ones, which already excludes incomplete rules, JS-only
    rules and ``singleUrl`` (WebView) sources -- 142 of the 1,344 archived.
    Requires ``POST /reader/scan?source_type=rss`` to have run at least once;
    before that the pool is empty and this returns nothing.
    """
    page = get_rss_service().available_sources(limit=limit, offset=offset, q=q)
    return RssSourcePage(
        items=[RssSourceOut(**item) for item in page["items"]],
        total=page["total"],
        limit=page["limit"],
        offset=page["offset"],
    )


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


@router.get("/subscriptions", response_model=list[SubscriptionOut])
def list_subscriptions(user: CurrentUser = Depends(require_user)) -> list[SubscriptionOut]:
    return [SubscriptionOut(**item) for item in get_rss_service().subscriptions(user.user_id)]


@router.post("/subscriptions", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
def subscribe(
    payload: SubscribeIn,
    user: CurrentUser = Depends(require_user),
) -> SubscriptionOut:
    """Subscribe. Idempotent -- re-subscribing updates the existing row.

    See the module docstring on why this is behind ``require_user``: for
    ``kind="feed"`` the server fetches the URL the caller supplies.
    """
    service = get_rss_service()
    if payload.kind == KIND_LEGADO:
        if payload.url_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="kind=legado 需要 url_id",
            )
        with engine_errors():
            result = service.subscribe_legado(payload.url_id, user.user_id, title=payload.title)
    else:
        if not payload.feed_url.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="kind=feed 需要 feed_url",
            )
        #  A bad feed URL is the caller's typo, not a server fault, so the
        #  parse error comes back as 400 with the reason rather than a 502.
        try:
            result = service.subscribe_feed(payload.feed_url, user.user_id, title=payload.title)
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"无法订阅这个地址：{error}",
            ) from error
    return SubscriptionOut(**result)


@router.delete(
    "/subscriptions/{sub_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
def unsubscribe(sub_id: str, user: CurrentUser = Depends(require_user)) -> None:
    """Unsubscribe. Also drops this user's read/favourite state for it."""
    if not get_rss_service().unsubscribe(sub_id, user.user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这个订阅")


@router.get("/subscriptions/{sub_id}/categories", response_model=list[CategoryOut])
def categories(sub_id: str, user: CurrentUser = Depends(require_user)) -> list[CategoryOut]:
    """``sortUrl`` entries for a Legado source; one pseudo-entry for a feed."""
    with engine_errors():
        return [CategoryOut(**item) for item in get_rss_service().categories(sub_id, user.user_id)]


@router.put("/subscriptions/{sub_id}/read-all", response_model=ReadAllOut)
def read_all(
    sub_id: str,
    payload: ReadAllIn,
    user: CurrentUser = Depends(require_user),
) -> ReadAllOut:
    """Mark a batch of articles read.

    The caller supplies the keys rather than the server saying "everything in
    this subscription": the server does not know what a subscription contains
    (it caches no article list), and the UI's "mark all read" means "the ones
    on screen" anyway.
    """
    service = get_rss_service()
    #  Scoped so one user cannot mark keys under someone else's subscription.
    with engine_errors():
        service.subscription(sub_id, user.user_id)
    return ReadAllOut(changed=service.mark_all_read(sub_id, user.user_id, payload.article_keys))


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------


@router.get("/articles", response_model=ArticlePage)
def list_articles(
    sub_id: str = Query(min_length=1),
    category: str = Query(default="", max_length=512),
    next_url: str = Query(default="", max_length=2048),
    page: int = Query(default=1, ge=1, le=1000),
    limit: int = Query(default=30, ge=1, le=MAX_ARTICLES_PER_PAGE),
    user: CurrentUser = Depends(require_user),
) -> ArticlePage:
    """One page of articles, each carrying this user's read/favourite state.

    ``next_url`` takes precedence over ``category`` -- it is literally the
    "next page" address the previous call returned.
    """
    with engine_errors():
        result = get_rss_service().articles(
            sub_id,
            user_id=user.user_id,
            category=category,
            next_url=next_url,
            page=page,
            limit=limit,
        )
    return ArticlePage(
        items=[ArticleOut(**item) for item in result["items"]],
        next_url=result["next_url"],
        category=result["category"],
        has_content=result["has_content"],
    )


@router.get("/article", response_model=ArticleDetail)
def read_article(
    sub_id: str = Query(min_length=1),
    link: str = Query(min_length=1, max_length=2048),
    title: str = Query(default="", max_length=512),
    variables: str = Query(
        default="",
        max_length=4096,
        description=(
            "列表响应里那一项的 variables，JSON 对象。规则可以在列表页 @put、"
            "在正文页 @get —— 不回传这些源会静默读到空正文。"
        ),
    ),
    user: CurrentUser = Depends(require_user),
) -> ArticleDetail:
    """One article's text.

    GET with the link in the query string rather than POST with a body: it is
    a read, and being linkable matters -- the front-end router puts the link in
    the URL so a reload lands back on the same article. ``variables`` therefore
    rides along JSON-encoded rather than as a request body.

    Round-tripping ``variables`` matters for the same reason it does on the book
    side: rules capture values with ``@put`` on the list page and read them back
    with ``@get`` on the article page. Only 2 archived sources do this today and
    neither is currently runnable (both need JS), but the engine already emits
    the values -- dropping them here would mean those sources break silently the
    day a JS runtime lands.
    """
    parsed = _parse_variables(variables)
    with engine_errors():
        return ArticleDetail(
            **get_rss_service().article(sub_id, user.user_id, link, variables=parsed, title=title)
        )


@router.put(
    "/subscriptions/{sub_id}/articles/{article_key}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def mark_read(
    sub_id: str,
    article_key: str,
    payload: ReadIn,
    user: CurrentUser = Depends(require_user),
) -> None:
    service = get_rss_service()
    with engine_errors():
        service.subscription(sub_id, user.user_id)
    service.mark_read(sub_id, user.user_id, article_key, read=payload.read, meta=_meta(payload))


@router.put(
    "/subscriptions/{sub_id}/articles/{article_key}/favorite",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def mark_favorite(
    sub_id: str,
    article_key: str,
    payload: FavoriteIn,
    user: CurrentUser = Depends(require_user),
) -> None:
    service = get_rss_service()
    with engine_errors():
        service.subscription(sub_id, user.user_id)
    service.mark_favorite(
        sub_id, user.user_id, article_key, favorited=payload.favorited, meta=_meta(payload)
    )


@router.get("/favorites", response_model=list[FavoriteOut])
def favorites(
    sub_id: str = Query(default="", max_length=32),
    user: CurrentUser = Depends(require_user),
) -> list[FavoriteOut]:
    """Favourited articles. Renders from the stored snapshot, not the feed --
    a favourite must survive the article scrolling off the source's first page."""
    return [
        FavoriteOut(**item) for item in get_rss_service().favorites(user.user_id, sub_id=sub_id)
    ]
