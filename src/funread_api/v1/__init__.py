from fastapi import APIRouter, Depends

from funread_api.security import require_reader, require_session, require_user

from .auth import router as auth_router
from .reader import router as reader_router
from .shelf import router as shelf_router
from .sources import router as sources_router

api_router = APIRouter()

#  /auth is the way in, so it cannot require a session itself.
api_router.include_router(auth_router)

#  Management endpoints always need the admin password when one is configured.
#  POST /sources in particular fetches an arbitrary URL server-side (SSRF), so
#  it must never be reachable via the FUNREAD_READER_PUBLIC escape hatch, and
#  never via a mere reader account either -- reading is not administering.
api_router.include_router(sources_router, dependencies=[Depends(require_session)])

#  The reader flow is stateless (search / parse / fetch) and may be opened up
#  read-only with FUNREAD_READER_PUBLIC=1; writes still need an identity.
api_router.include_router(reader_router, dependencies=[Depends(require_reader)])

#  /shelf is per-user state throughout, so it needs a reader identity and never
#  opens up. The guard is repeated on each endpoint because they also need the
#  CurrentUser value itself; FastAPI caches the dependency per request, so it
#  resolves once.
api_router.include_router(shelf_router, dependencies=[Depends(require_user)])

__all__ = ["api_router"]
