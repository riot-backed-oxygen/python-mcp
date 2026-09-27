"""Bearer-token authentication for the streamable-http transport.

Why a shared secret rather than the OAuth machinery the MCP SDK also offers:
this server is a personal tool that runs arbitrary code on one machine. Full
OAuth 2.1 would mean standing up an authorization server to issue tokens to
yourself, and the SDK's static-verifier path still demands an ``issuer_url`` --
a URL that would have to point somewhere that does not exist. A single token,
compared in constant time, is the honest fit for that shape and is what MCP
clients can send with no extra configuration.

The token is read only from the ``Authorization`` header, never from a query
string, so it stays out of access logs and ``Referer`` headers.
"""

from __future__ import annotations

import hmac
import json
import logging

logger = logging.getLogger(__name__)

REALM = "pyexec-mcp"


def _header(scope: dict, name: bytes) -> str | None:
    """Case-insensitive lookup of a single request header."""
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def presented_token(scope: dict) -> str | None:
    """The bearer token on *scope*, or ``None`` if it carried none."""
    raw = _header(scope, b"authorization")
    if not raw:
        return None
    scheme, _, credentials = raw.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credentials.strip() or None


async def _reject(send, had_token: bool) -> None:
    """Answer with a spec-shaped 401."""
    if had_token:
        error = "invalid_token"
        description = "The bearer token is not valid."
    else:
        error = "invalid_request"
        description = "Send the token as 'Authorization: Bearer <token>'."

    body = json.dumps({"error": error, "error_description": description}).encode()
    challenge = (
        f'Bearer realm="{REALM}", error="{error}", '
        f'error_description="{description}"'
    )

    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", challenge.encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerTokenMiddleware:
    """Pure-ASGI middleware that requires a matching bearer token.

    Deliberately dependency-free rather than built on Starlette's
    ``BaseHTTPMiddleware``: it sits in front of the MCP session endpoint, and a
    middleware that streams responses incorrectly would break the protocol in
    ways that are painful to debug.
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._expected = token.encode("utf-8")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            # Lifespan and websocket scopes are not client requests.
            await self.app(scope, receive, send)
            return

        token = presented_token(scope)
        if token is not None and hmac.compare_digest(
            token.encode("utf-8"), self._expected
        ):
            await self.app(scope, receive, send)
            return

        client = scope.get("client") or ()
        logger.warning(
            "Rejected unauthenticated %s %s from %s",
            scope.get("method"),
            scope.get("path"),
            client[0] if client else "unknown",
        )
        await _reject(send, had_token=token is not None)


__all__ = ["BearerTokenMiddleware", "presented_token", "REALM"]
