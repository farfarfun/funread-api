"""Synchronous helpers for setting up accounts in tests.

funauth is async-only, but most of this suite is sync ``TestClient`` code that
just needs "an account exists" as a precondition. These wrap the async calls in
``asyncio.run`` and always dispose the engine afterwards -- the same shape as
``cli.py``'s ``_with_session``, and for the same reason: every ``TestClient``
context runs on its own event loop, so a cached connection must not outlive the
loop that opened it.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets

from funauth import UserRole

from funread_api.accounts import (
    accounts,
    get_async_session_factory,
    init_auth_db,
    reset_async_engines,
)


def _run(action):
    async def main():
        init_auth_db()
        factory = get_async_session_factory()
        try:
            async with factory() as session:
                return await action(session)
        finally:
            await reset_async_engines()

    return asyncio.run(main())


def make_account(
    username: str = "alice",
    password: str = "password123",
    role: UserRole = UserRole.GUEST,
) -> int:
    """Create an account the way the CLI does, and return its id."""
    return _run(lambda session: accounts.create_user(session, username, password, role)).id


def issue_invite(max_uses: int = 1) -> str:
    """Issue a registration invite code and return the code value."""
    return _run(lambda session: accounts.issue_invite(session, max_uses=max_uses)).code


def set_active(username: str, active: bool) -> None:
    """Enable/disable an account. Disabling is how an account is taken away."""
    _run(lambda session: accounts.set_active(session, username, active))


def set_password_hash(username: str, password_hash: str) -> None:
    """Overwrite a stored hash directly -- used to plant a pre-funauth one."""

    async def action(session):
        user = await accounts.get_by_username(session, username)
        user.password_hash = password_hash
        await session.commit()

    _run(action)


def read_password_hash(username: str) -> str:
    return _run(lambda session: accounts.get_by_username(session, username)).password_hash


def legacy_scrypt_hash(password: str) -> str:
    """Build a hash in the pre-funauth format, to test the transparent upgrade.

    Deliberately duplicated here rather than imported: the production hasher is
    *gone*, and the point of this test is that the verifier still reads what the
    old one wrote. A shared helper would let both drift together and still pass.
    """
    salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 * 1024 * 1024
    )
    return f"scrypt${n}${r}${p}${salt.hex()}${key.hex()}"
