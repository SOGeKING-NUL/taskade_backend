"""
Auth service — verifies Auth0-issued ID tokens.

The frontend (Auth0 React SDK, Authorization Code + PKCE) handles login
entirely client-side, including the Google social connection — this backend
never talks to Auth0's token endpoint or Google directly. Its only job is to
verify the ID token's signature against Auth0's public JWKS and read the
user id (`sub`) plus profile fields (`email`, `name`) out of its claims.

We verify the ID token (not an access token) because no separate Auth0 API
resource is configured — the ID token's `aud` is simply the Auth0 client id,
which is enough to authenticate "who is this" without a full resource-server
setup. RS256 + JWKS (asymmetric) — there's no shared secret to leak.

REST endpoints use `get_current_user_id` (reads `Authorization: Bearer <jwt>`).
Browsers can't attach custom headers to a WebSocket upgrade request, so
`/ws/voice` instead accepts the token as a `?token=` query param, verified the
same way via `authenticate_websocket` before the socket is accepted.
"""

import logging

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.websockets import WebSocket

from core.config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
_jwks_client = jwt.PyJWKClient(f"https://{settings.AUTH0_DOMAIN}/.well-known/jwks.json")


class AuthError(Exception):
    pass


def decode_token(token: str) -> dict:
    """Verify an Auth0 ID token (RS256, via JWKS) and return its claims."""
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.AUTH0_CLIENT_ID,
            issuer=f"https://{settings.AUTH0_DOMAIN}/",
        )
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc


def profile_fields(claims: dict) -> dict:
    """Pull Google-sign-in profile fields out of a verified token's claims."""
    return {
        "email": claims.get("email"),
        "display_name": claims.get("name") or claims.get("nickname"),
    }


async def get_current_claims(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """FastAPI dependency returning the FULL verified token claims.

    Use this (instead of `get_current_user_id`) when an endpoint needs the
    caller's identity fields — name/email — not just their id, e.g. the
    login-time profile sync.
    """
    if creds is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        return decode_token(creds.credentials)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc


async def get_current_user_id(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """FastAPI dependency for REST endpoints — returns the verified user id."""
    if creds is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        claims = decode_token(creds.credentials)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    return claims["sub"]


async def authenticate_websocket(ws: WebSocket) -> dict | None:
    """
    Verify the `?token=` query param before the socket is accepted.

    Returns the token's claims on success. On failure, closes the socket
    (denying the connection per the ASGI websocket spec — no accept() needed
    first) and returns None; the caller should just return immediately.
    """
    token = ws.query_params.get("token")
    if not token:
        logger.warning("WS connect rejected — no token")
        await ws.close(code=1008)
        return None
    try:
        return decode_token(token)
    except AuthError as exc:
        logger.warning("WS connect rejected — invalid token: %s", exc)
        await ws.close(code=1008)
        return None
