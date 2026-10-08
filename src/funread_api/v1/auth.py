"""Login / logout / session probe for the pre-shared password."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from funread_api.security import (
    COOKIE_NAME,
    SESSION_TTL,
    auth_enabled,
    is_authenticated,
    issue_token,
    reader_is_public,
    resolve_api_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class SessionState(BaseModel):
    auth_required: bool
    authenticated: bool
    reader_public: bool


def _state(request: Request) -> SessionState:
    return SessionState(
        auth_required=auth_enabled(),
        authenticated=is_authenticated(request),
        reader_public=reader_is_public(),
    )


@router.get("/me", response_model=SessionState)
def me(request: Request) -> SessionState:
    """Lets the front-end decide whether to show a login screen at all."""
    return _state(request)


@router.post("/login", response_model=SessionState)
def login(payload: LoginRequest, request: Request, response: Response) -> SessionState:
    password = resolve_api_password()
    if password is None:
        # Nothing to log into. Say so rather than handing out a cookie that no
        # dependency would ever check.
        return _state(request)

    if not payload.password or payload.password != password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="口令不正确")

    response.set_cookie(
        key=COOKIE_NAME,
        value=issue_token(password),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
        #  Deliberately not secure=True: this is self-hosted and normally
        #  reached over plain http on a LAN IP, where a secure cookie would
        #  simply never be sent back.
        secure=False,
    )
    return SessionState(auth_required=True, authenticated=True, reader_public=reader_is_public())


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def logout(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")
