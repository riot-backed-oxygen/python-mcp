"""Time-limited download links for files in the workspace.

The MCP endpoint is guarded by a bearer token in a header; a download link is
guarded by a signature in the URL instead. The difference is the use case: the
whole point of handing back a link is that a person can click it, and a browser
navigation cannot carry an ``Authorization`` header. Signing also keeps the
token itself out of the URL, where it would leak into access logs, ``Referer``
headers and browser history -- the same reason :mod:`pyexec_mcp.auth` refuses to
read the token from a query string.

A signature covers one path and one expiry, so a link that escapes exposes a
single file for a bounded time rather than the whole workspace forever. It is
derived from ``PYEXEC_AUTH_TOKEN``, so rotating the token invalidates every
outstanding link. With no token configured the download route asks for no
signature at all, which is not a hole: the startup interlock in
:func:`pyexec_mcp.server.run_http` refuses to bind a non-loopback address
without a token, so such a server is reachable only from this machine.

The route is served by a front door in front of the MCP app rather than a
Starlette ``Mount``. Mounting was tried and does not work: Starlette does not
forward lifespan events to a mounted sub-application, so the streamable-http
session manager never starts and every request fails with ``Task group is not
initialized``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from starlette.responses import FileResponse, PlainTextResponse

from .auth import presented_token
from .config import Settings
from .sandbox import GuardrailError, resolve_within

logger = logging.getLogger(__name__)

#: Path prefix every download link starts with.
DOWNLOAD_PREFIX = "/download/"

#: Methods a download link may be fetched with. HEAD is included so a client can
#: learn the size before committing to the transfer.
ALLOWED_METHODS = ("GET", "HEAD")


def _message(path: str, expires: int) -> bytes:
    """The exact bytes a signature covers.

    The newline is what keeps the two fields from running together: without it
    ``("a1", 23)`` and ``("a", 123)`` would sign identically.
    """
    return f"{path}\n{expires}".encode("utf-8")


def sign_path(token: str, path: str, expires: int) -> str:
    """Sign *path*'s availability until *expires*, as a hex digest."""
    return hmac.new(
        token.encode("utf-8"), _message(path, expires), hashlib.sha256
    ).hexdigest()


def verify(
    token: str, path: str, expires: int, signature: str, now: float | None = None
) -> bool:
    """Whether *signature* authorizes *path* and has not lapsed.

    Expiry is checked before the digest, so a stale link is rejected without
    pretending the signature was the problem. The comparison itself is constant
    time, as in :mod:`pyexec_mcp.auth`.
    """
    if expires <= (time.time() if now is None else now):
        return False
    expected = sign_path(token, path, expires).encode("ascii")
    presented = signature.encode("utf-8", "replace")
    return hmac.compare_digest(expected, presented)


def workspace_relative(target: Path, workspace: Path) -> str:
    """*target* as a workspace-relative posix path.

    This -- not whatever the client typed into the URL -- is what a link signs,
    so ``%2e%2e%2f``, doubled slashes and other spellings of the same file all
    canonicalise to one signed string. ``/`` separators keep links identical on
    Windows and elsewhere.
    """
    return target.relative_to(workspace).as_posix()


def build_url(
    base_url: str,
    path: str,
    token: str | None,
    ttl_seconds: float,
    now: float | None = None,
) -> tuple[str, int]:
    """Build a download URL for *path*, and the moment it stops working.

    Args:
        base_url: Where clients reach this server, without a trailing slash.
        path: Workspace-relative posix path, as returned by
            :func:`workspace_relative`.
        token: Signing key. ``None`` when the server is unauthenticated, in
            which case the link carries no query string -- see the module
            docstring for why that is safe.
        ttl_seconds: How long the link stays valid.
        now: Current time, for tests.

    Returns:
        ``(url, expires_at)`` with ``expires_at`` as a Unix timestamp.
    """
    expires = int((time.time() if now is None else now) + ttl_seconds)
    url = f"{base_url.rstrip('/')}{DOWNLOAD_PREFIX}{quote(path, safe='/')}"
    if token is not None:
        url = f"{url}?{urlencode({'exp': expires, 'sig': sign_path(token, path, expires)})}"
    return url, expires


async def _fail(scope, receive, send, status: int, message: str, **headers: str) -> None:
    """Answer with a short reason a human reading a broken link can act on."""
    response = PlainTextResponse(message + "\n", status_code=status)
    for name, value in headers.items():
        response.headers[name.replace("_", "-")] = value
    await response(scope, receive, send)


class DownloadRouter:
    """Serve ``/download/<path>`` and pass everything else to the MCP app.

    Sits *outside* :class:`~pyexec_mcp.auth.BearerTokenMiddleware` on purpose: a
    download link carries its authority in the URL, so a bearer gate in front of
    it would reject exactly the requests it exists to serve. The gate still
    covers the MCP endpoint, because that is the app this hands off to.

    The configuration is a snapshot taken when the server starts, matching the
    "resolved once from the environment" rule the rest of the settings follow.
    """

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(DOWNLOAD_PREFIX):
            # Lifespan, websockets and every other path belong to the MCP app.
            await self.app(scope, receive, send)
            return
        await self._serve(scope, receive, send)

    async def _serve(self, scope, receive, send) -> None:
        if scope["method"] not in ALLOWED_METHODS:
            await _fail(
                scope,
                receive,
                send,
                405,
                "Downloads are served over GET and HEAD only.",
                allow=", ".join(ALLOWED_METHODS),
            )
            return

        # The ASGI server has already percent-decoded the path, so this is a
        # plain file path and resolve_within sees a real "../" if there is one.
        relative = scope["path"][len(DOWNLOAD_PREFIX) :]
        try:
            target = resolve_within(relative, self.settings.workspace)
        except GuardrailError:
            await _fail(scope, receive, send, 403, "That path is outside the workspace.")
            return

        signed_path = workspace_relative(target, self.settings.workspace)
        if not self._authorized(scope, signed_path):
            logger.warning(
                "Rejected download of %s from %s",
                signed_path,
                (scope.get("client") or ("unknown",))[0],
            )
            await _fail(
                scope, receive, send, 401, "That download link is invalid or has expired."
            )
            return

        if not target.is_file():
            await _fail(scope, receive, send, 404, "No such file.")
            return

        # FileResponse brings streaming, Range (so a download can be resumed),
        # ETag and a media type guessed from the extension; passing filename
        # turns it into an attachment with the original name.
        await FileResponse(target, filename=target.name)(scope, receive, send)

    def _authorized(self, scope, signed_path: str) -> bool:
        """Whether the request proves it may read *signed_path*."""
        token = self.settings.auth_token
        if token is None:
            # Unauthenticated means loopback-only, enforced at startup.
            return True

        # A script can simply send the token it already has; a browser cannot,
        # which is what the signature in the URL is for.
        presented = presented_token(scope)
        if presented is not None and hmac.compare_digest(
            presented.encode("utf-8"), token.encode("utf-8")
        ):
            return True

        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        try:
            expires = int((query.get("exp") or [""])[0])
        except ValueError:
            return False
        return verify(token, signed_path, expires, (query.get("sig") or [""])[0])


__all__ = [
    "ALLOWED_METHODS",
    "DOWNLOAD_PREFIX",
    "DownloadRouter",
    "build_url",
    "sign_path",
    "verify",
    "workspace_relative",
]
