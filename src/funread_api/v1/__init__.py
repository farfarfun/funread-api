from fastapi import APIRouter, Depends

from funread_api.security import require_reader, require_reader_admin, require_user

from .auth import router as auth_router
from .pool import router as pool_router
from .reader import router as reader_router
from .rss import router as rss_router
from .shelf import router as shelf_router
from .sources import router as sources_router

api_router = APIRouter()

#  /auth is the way in, so it cannot require a session itself.
api_router.include_router(auth_router)

#  管理端按**账号角色**鉴权：必须是登录着的 ADMIN。
#
#  以前这里是 `require_session` —— 一个与账号体系平行的预共享口令（[auth]
#  password）。两套凭据有个要命的缺省行为：口令没配时 `is_authenticated` 恒为
#  真，于是整个管理端对任何人敞开。一台绑到 0.0.0.0 的实例就这么把采集源管理
#  暴露在公网上，而服务只在启动日志里提了一句。
#
#  角色判断没有这个缺口：没登录就是 401，不是 ADMIN 就是 403，不存在「没配就
#  全开」的默认值。谁能管也从「谁知道那个口令」变成「库里哪个账号是 admin」，
#  可查可改可回收。
#
#  POST /sources 会让服务端去抓调用方给的任意 URL（SSRF），/pool 改一个源的启停
#  会影响**所有用户**的搜索结果 —— 两者都不该被 [auth] reader_public 放开，也不
#  该由普通读者账号够得着：能读不等于能管。
api_router.include_router(sources_router, dependencies=[Depends(require_reader_admin)])
api_router.include_router(pool_router, dependencies=[Depends(require_reader_admin)])

#  The reader flow is stateless (search / parse / fetch) and may be opened up
#  read-only with [auth] reader_public = true; writes still need an identity.
api_router.include_router(reader_router, dependencies=[Depends(require_reader)])

#  /shelf is per-user state throughout, so it needs a reader identity and never
#  opens up. The guard is repeated on each endpoint because they also need the
#  CurrentUser value itself; FastAPI caches the dependency per request, so it
#  resolves once.
api_router.include_router(shelf_router, dependencies=[Depends(require_user)])

#  /rss is per-user state too, and POST /rss/subscriptions makes the server
#  fetch a URL the caller supplies (SSRF). Same guard as /shelf: authenticated
#  reader only, never opened by [auth] reader_public.
api_router.include_router(rss_router, dependencies=[Depends(require_user)])

__all__ = ["api_router"]
