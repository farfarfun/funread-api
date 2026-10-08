from fastapi import APIRouter, Depends

from funread_api.security import require_reader, require_session

from .auth import router as auth_router
from .reader import router as reader_router
from .shelf import router as shelf_router
from .sources import router as sources_router

api_router = APIRouter()

#  /auth is the way in, so it cannot require a session itself.
api_router.include_router(auth_router)

#  Management endpoints always need a session when a password is configured.
#  POST /sources in particular fetches an arbitrary URL server-side (SSRF), so
#  it must never be reachable via the FUNREAD_READER_PUBLIC escape hatch.
api_router.include_router(sources_router, dependencies=[Depends(require_session)])

#  The reader side may be opened up read-only (FUNREAD_READER_PUBLIC=1); writes
#  still need the cookie. /shelf is all per-user state, so it never opens up.
api_router.include_router(reader_router, dependencies=[Depends(require_reader)])
api_router.include_router(shelf_router, dependencies=[Depends(require_session)])

__all__ = ["api_router"]
