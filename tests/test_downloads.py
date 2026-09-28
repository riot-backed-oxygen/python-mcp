"""Tests for signed file-download links.

The interesting properties are all adversarial ones: a link must not survive a
changed path, a changed expiry, a different token, or the passage of time, and
the path cage the other file tools use must still hold.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest

from pyexec_mcp import server
from pyexec_mcp.config import DEFAULT_DOWNLOAD_TTL, Settings, derive_base_url
from pyexec_mcp.downloads import (
    DOWNLOAD_PREFIX,
    DownloadRouter,
    build_url,
    sign_path,
    verify,
    workspace_relative,
)

TOKEN = "correct-horse-battery-staple-32chars"
BASE = "http://127.0.0.1:8123"


# --------------------------------------------------------------------------
# ASGI plumbing, in the style of test_auth.py
# --------------------------------------------------------------------------

class Recorder:
    """Collects the ASGI messages a response emits."""

    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return self.messages[0]["status"]

    @property
    def headers(self):
        return {name.decode(): value.decode() for name, value in self.messages[0]["headers"]}

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.messages[1:])


class InnerApp:
    """Stands in for the MCP app; records that it was reached."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"mcp"})


def http_scope(path, query="", method="GET", headers=()):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query.encode("latin-1"),
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in headers
        ],
        "client": ("127.0.0.1", 54321),
        "extensions": {},
    }


async def fetch(router, scope):
    """Drive *router* with *scope* and return the recorder."""
    send = Recorder()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await router(scope, receive, send)
    return send


@pytest.fixture
def file_tree(tmp_path):
    """A workspace with a file in it, and a second file outside the workspace."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.txt").write_bytes(b"col1,col2\n1,2\n")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "outside.txt").write_bytes(b"not yours")
    return workspace


@pytest.fixture
def router(file_tree, make_settings):
    return DownloadRouter(InnerApp(), make_settings(file_tree, auth_token=TOKEN, base_url=BASE))


# --------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------

def test_a_fresh_link_verifies():
    url, expires = build_url(BASE, "report.txt", TOKEN, 60)

    assert url.startswith(f"{BASE}{DOWNLOAD_PREFIX}report.txt?")
    assert verify(TOKEN, "report.txt", expires, sign_path(TOKEN, "report.txt", expires))


def test_the_signature_covers_the_path():
    _, expires = build_url(BASE, "report.txt", TOKEN, 60)
    signature = sign_path(TOKEN, "report.txt", expires)

    assert verify(TOKEN, "secrets.txt", expires, signature) is False


def test_the_signature_covers_the_expiry():
    _, expires = build_url(BASE, "report.txt", TOKEN, 60)
    signature = sign_path(TOKEN, "report.txt", expires)

    assert verify(TOKEN, "report.txt", expires + 86_400, signature) is False


def test_a_different_token_does_not_verify():
    _, expires = build_url(BASE, "report.txt", TOKEN, 60)
    signature = sign_path(TOKEN, "report.txt", expires)

    assert verify("another-token-entirely", "report.txt", expires, signature) is False


def test_a_lapsed_link_does_not_verify():
    # Signed correctly, but its window has already closed.
    expires = int(time.time()) - 1

    assert verify(TOKEN, "report.txt", expires, sign_path(TOKEN, "report.txt", expires)) is False


def test_expiry_and_path_cannot_be_confused_for_one_another():
    # The signed message separates its fields, so a path ending in a number
    # cannot impersonate a different expiry.
    url_a = sign_path(TOKEN, "a1", 23)
    url_b = sign_path(TOKEN, "a", 123)

    assert url_a != url_b


def test_an_unauthenticated_server_produces_an_unsigned_link():
    url, _ = build_url(BASE, "report.txt", None, 60)

    assert url == f"{BASE}{DOWNLOAD_PREFIX}report.txt"
    assert "sig=" not in url


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    url, _ = build_url(f"{BASE}/", "report.txt", TOKEN, 60)

    assert url.startswith(f"{BASE}{DOWNLOAD_PREFIX}")


@pytest.mark.parametrize(
    "path, quoted",
    [
        ("my report.txt", "my%20report.txt"),
        ("sub/chart.png", "sub/chart.png"),
        ("图表.csv", "%E5%9B%BE%E8%A1%A8.csv"),
    ],
)
def test_paths_are_quoted_but_signed_in_the_clear(path, quoted):
    # The URL is percent-encoded; the signature is over the real path, so the
    # router does not have to guess how the client encoded it.
    url, expires = build_url(BASE, path, TOKEN, 60)

    assert url.startswith(f"{BASE}{DOWNLOAD_PREFIX}{quoted}?")
    assert verify(TOKEN, path, expires, sign_path(TOKEN, path, expires))


# --------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_valid_link_serves_the_file(router, file_tree):
    url, _ = build_url(BASE, "report.txt", TOKEN, 60)
    parts = urlsplit(url)

    send = await fetch(router, http_scope(parts.path, parts.query))

    assert send.status == 200
    assert send.body == (file_tree / "report.txt").read_bytes()
    assert send.headers["content-disposition"] == 'attachment; filename="report.txt"'


@pytest.mark.asyncio
async def test_nested_paths_are_served(router):
    url, _ = build_url(BASE, "sub/chart.png", TOKEN, 60)
    parts = urlsplit(url)

    send = await fetch(router, http_scope(parts.path, parts.query))

    assert send.status == 200
    assert send.body.startswith(b"\x89PNG")
    assert send.headers["content-type"] == "image/png"


@pytest.mark.asyncio
async def test_head_reports_the_size_without_the_body(router):
    url, _ = build_url(BASE, "report.txt", TOKEN, 60)
    parts = urlsplit(url)

    send = await fetch(router, http_scope(parts.path, parts.query, method="HEAD"))

    assert send.status == 200
    assert "content-length" in send.headers
    assert send.body == b""


@pytest.mark.asyncio
async def test_a_link_with_no_signature_is_refused(router, file_tree):
    send = await fetch(router, http_scope(f"{DOWNLOAD_PREFIX}report.txt"))

    assert send.status == 401
    assert send.body != (file_tree / "report.txt").read_bytes()


@pytest.mark.asyncio
async def test_a_tampered_signature_is_refused(router):
    url, expires = build_url(BASE, "report.txt", TOKEN, 60)
    parts = urlsplit(url)
    signature = sign_path(TOKEN, "report.txt", expires)

    send = await fetch(
        router,
        http_scope(
            parts.path, f"exp={expires}&sig={'0' * len(signature)}"
        ),
    )

    assert send.status == 401


@pytest.mark.asyncio
async def test_a_signature_for_another_file_is_refused(router):
    # The attack a naive implementation would allow: reuse a signature you have
    # for one path to fetch a different one.
    _, expires = build_url(BASE, "report.txt", TOKEN, 60)

    send = await fetch(
        router,
        http_scope(
            f"{DOWNLOAD_PREFIX}sub/chart.png",
            f"exp={expires}&sig={sign_path(TOKEN, 'report.txt', expires)}",
        ),
    )

    assert send.status == 401


@pytest.mark.asyncio
async def test_an_expired_link_is_refused(router):
    url, expires = build_url(BASE, "report.txt", TOKEN, -1)
    parts = urlsplit(url)

    send = await fetch(router, http_scope(parts.path, parts.query))

    assert send.status == 401


@pytest.mark.asyncio
async def test_a_malformed_expiry_is_refused(router):
    send = await fetch(
        router, http_scope(f"{DOWNLOAD_PREFIX}report.txt", "exp=soon&sig=whatever")
    )

    assert send.status == 401


@pytest.mark.asyncio
async def test_escaping_the_workspace_is_refused(router):
    # Signed for a path that resolves outside the workspace: the cage, not the
    # signature, is what has to stop this.
    _, expires = build_url(BASE, "../outside.txt", TOKEN, 60)

    send = await fetch(
        router,
        http_scope(
            f"{DOWNLOAD_PREFIX}../outside.txt",
            f"exp={expires}&sig={sign_path(TOKEN, '../outside.txt', expires)}",
        ),
    )

    assert send.status == 403
    assert b"not yours" not in send.body


@pytest.mark.asyncio
async def test_a_signed_but_missing_file_is_a_404(router):
    url, _ = build_url(BASE, "gone.txt", TOKEN, 60)
    parts = urlsplit(url)

    send = await fetch(router, http_scope(parts.path, parts.query))

    assert send.status == 404


@pytest.mark.asyncio
async def test_the_bearer_token_works_instead_of_a_signature(router):
    # For a script that already holds the token, the header is simpler than
    # signing a URL.
    send = await fetch(
        router,
        http_scope(
            f"{DOWNLOAD_PREFIX}report.txt",
            headers=[("Authorization", f"Bearer {TOKEN}")],
        ),
    )

    assert send.status == 200


@pytest.mark.asyncio
async def test_a_wrong_bearer_token_is_refused(router):
    send = await fetch(
        router,
        http_scope(
            f"{DOWNLOAD_PREFIX}report.txt",
            headers=[("Authorization", "Bearer wrong-token-entirely")],
        ),
    )

    assert send.status == 401


@pytest.mark.asyncio
async def test_only_get_and_head_are_served(router):
    url, _ = build_url(BASE, "report.txt", TOKEN, 60)
    parts = urlsplit(url)

    send = await fetch(router, http_scope(parts.path, parts.query, method="DELETE"))

    assert send.status == 405
    assert send.headers["allow"] == "GET, HEAD"


@pytest.mark.asyncio
async def test_an_unauthenticated_server_serves_unsigned_links(file_tree, make_settings):
    # No token means the startup interlock confined the server to loopback, so
    # there is nothing to sign with and nothing to protect against.
    router = DownloadRouter(InnerApp(), make_settings(file_tree, auth_token=None, base_url=BASE))

    send = await fetch(router, http_scope(f"{DOWNLOAD_PREFIX}report.txt"))

    assert send.status == 200


@pytest.mark.asyncio
async def test_other_paths_reach_the_mcp_app(router):
    inner = router.app

    send = await fetch(router, http_scope("/mcp", method="POST"))

    assert inner.called is True
    assert send.body == b"mcp"


@pytest.mark.asyncio
async def test_lifespan_reaches_the_mcp_app(router):
    # Wrapping the app must not strand the streamable-http session manager's
    # startup; that is why this is an ASGI front door and not a Mount.
    inner = router.app
    send = Recorder()

    await router({"type": "lifespan"}, None, send)

    assert inner.called is True


@pytest.mark.asyncio
async def test_the_bare_prefix_is_refused_rather_than_crashing(router):
    # "/download/" with nothing after it resolves to the workspace directory
    # itself: not a file, and not signed either.
    send = await fetch(
        router, http_scope(DOWNLOAD_PREFIX, f"exp={int(time.time()) + 60}&sig=x")
    )

    assert send.status == 401


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------

@pytest.fixture
def configured(tmp_path, make_settings):
    """Point the server at a workspace whose settings advertise a base URL."""
    current = make_settings(
        tmp_path, base_url=BASE, auth_token=TOKEN, download_ttl=600.0
    )
    server.use_settings(current)
    yield current
    server.use_settings(make_settings(tmp_path))


def test_the_tool_returns_a_link_the_router_accepts(configured, tmp_path):
    # Bytes, not text: write_text translates newlines on Windows, and the size
    # assertion below is about what the link reports for a known file.
    (tmp_path / "summary.csv").write_bytes(b"a,b\n1,2\n")

    link = server.pyexec_get_download_url("summary.csv")
    parts = urlsplit(link.url)
    query = parse_qs(parts.query)

    assert link.filename == "summary.csv"
    assert link.size_bytes == 8
    assert link.expires_in_seconds <= 600
    assert parts.path == f"{DOWNLOAD_PREFIX}summary.csv"
    assert verify(TOKEN, "summary.csv", int(query["exp"][0]), query["sig"][0])


@pytest.mark.asyncio
async def test_the_tool_serves_what_it_advertised(configured, tmp_path):
    """End to end over the router: the link the tool builds downloads the file."""
    payload = b"binary\x00\x01\x02"
    (tmp_path / "blob.bin").write_bytes(payload)

    link = server.pyexec_get_download_url("blob.bin")
    parts = urlsplit(link.url)
    router = DownloadRouter(InnerApp(), configured)

    send = await fetch(router, http_scope(parts.path, parts.query))

    assert send.status == 200
    assert send.body == payload


def test_the_tool_refuses_a_path_outside_the_workspace(configured):
    with pytest.raises(server.ToolError, match="escapes the workspace"):
        server.pyexec_get_download_url("../outside.txt")


def test_the_tool_refuses_a_missing_file(configured):
    with pytest.raises(server.ToolError, match="No such file"):
        server.pyexec_get_download_url("nope.txt")


def test_the_tool_refuses_a_directory(configured, tmp_path):
    (tmp_path / "reports").mkdir()

    with pytest.raises(server.ToolError, match="No such file"):
        server.pyexec_get_download_url("reports")


def test_the_ttl_is_capped_at_the_configured_maximum(configured, tmp_path):
    (tmp_path / "summary.csv").write_text("x", encoding="utf-8")

    link = server.pyexec_get_download_url("summary.csv", ttl_seconds=86_400)

    assert link.expires_in_seconds <= 600


def test_the_ttl_defaults_to_the_configured_value(configured, tmp_path):
    (tmp_path / "summary.csv").write_text("x", encoding="utf-8")

    link = server.pyexec_get_download_url("summary.csv")

    assert 595 <= link.expires_in_seconds <= 600
    assert link.expires_at.endswith("Z")


def test_an_unsigned_server_says_so(tmp_path, make_settings):
    server.use_settings(make_settings(tmp_path, base_url=BASE, auth_token=None))
    (tmp_path / "summary.csv").write_text("x", encoding="utf-8")
    try:
        link = server.pyexec_get_download_url("summary.csv")
    finally:
        server.use_settings(make_settings(tmp_path))

    assert "sig=" not in link.url
    assert "loopback" in link.note


def test_no_base_url_is_an_actionable_error(tmp_path, make_settings):
    server.use_settings(make_settings(tmp_path, base_url=""))
    try:
        with pytest.raises(server.ToolError, match="PYEXEC_BASE_URL"):
            server.pyexec_get_download_url("anything.txt")
    finally:
        server.use_settings(make_settings(tmp_path))


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def test_base_url_is_taken_from_the_environment(monkeypatch):
    monkeypatch.setenv("PYEXEC_BASE_URL", "https://files.example.com/pyexec/")

    # Trailing slash stripped, so links do not end up with a doubled one.
    assert Settings.from_env().base_url == "https://files.example.com/pyexec"


def test_base_url_falls_back_to_the_bind_address(monkeypatch):
    monkeypatch.delenv("PYEXEC_BASE_URL", raising=False)
    monkeypatch.setenv("PYEXEC_HOST", "127.0.0.1")
    monkeypatch.setenv("PYEXEC_PORT", "9001")

    assert Settings.from_env().base_url == "http://127.0.0.1:9001"


def test_an_ipv6_loopback_bind_is_bracketed(monkeypatch):
    monkeypatch.delenv("PYEXEC_BASE_URL", raising=False)
    monkeypatch.setenv("PYEXEC_HOST", "::1")
    monkeypatch.setenv("PYEXEC_PORT", "9001")

    assert Settings.from_env().base_url == "http://[::1]:9001"


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_a_wildcard_bind_is_never_advertised_verbatim(host):
    # "Listen everywhere" is not an address anyone can dial.
    url = derive_base_url(host, 8000)

    assert "0.0.0.0" not in url
    assert url.startswith("http://")


def test_download_ttl_defaults_to_an_hour(monkeypatch):
    monkeypatch.delenv("PYEXEC_DOWNLOAD_TTL", raising=False)

    assert Settings.from_env().download_ttl == DEFAULT_DOWNLOAD_TTL


def test_download_ttl_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PYEXEC_DOWNLOAD_TTL", "90")

    assert Settings.from_env().download_ttl == 90.0


def test_clamp_download_ttl_is_a_ceiling(tmp_path, make_settings):
    current = make_settings(tmp_path, download_ttl=120.0)

    assert current.clamp_download_ttl(None) == 120.0
    assert current.clamp_download_ttl(0) == 120.0
    assert current.clamp_download_ttl(30) == 30.0
    assert current.clamp_download_ttl(9_999) == 120.0


# --------------------------------------------------------------------------
# Live server
# --------------------------------------------------------------------------

def test_a_link_from_the_live_server_downloads_the_file(live_server):
    payload = b"invoice,42\n"
    (live_server.workspace / "invoice.csv").write_bytes(payload)

    with httpx.Client(trust_env=False, timeout=10) as client:
        # A link the server itself would have issued, built with the same
        # secret and the address it advertises.
        url, _ = build_url(
            live_server.base_url, "invoice.csv", live_server.token, 60
        )
        response = client.get(url)

    assert response.status_code == 200
    assert response.content == payload
    assert "attachment" in response.headers["content-disposition"]


def test_the_live_server_refuses_a_tampered_link(live_server):
    (live_server.workspace / "invoice.csv").write_bytes(b"invoice,42\n")
    url, expires = build_url(live_server.base_url, "invoice.csv", live_server.token, 60)

    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.get(f"{url.split('?')[0]}?exp={expires}&sig={'0' * 64}")

    assert response.status_code == 401


def test_the_live_server_refuses_a_path_outside_the_workspace(live_server):
    outside = live_server.workspace.parent / "outside.txt"
    outside.write_bytes(b"not yours")

    with httpx.Client(trust_env=False, timeout=10) as client:
        # An absolute path rather than a "..": clients and servers disagree
        # about when to normalise dot segments out of a URL, but nobody
        # rewrites an absolute one, so this exercises the cage itself. No
        # signature is offered, and none is needed -- the cage is checked
        # before authorization, so an escaping path is refused outright.
        response = client.get(
            f"{live_server.base_url}{DOWNLOAD_PREFIX}{quote(str(outside))}"
        )

    assert response.status_code == 403
    assert b"not yours" not in response.content


def test_the_live_server_still_gates_mcp_behind_the_token(live_server):
    """The download route must not have opened a hole in the MCP endpoint."""
    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.post(
            live_server.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code == 401


def test_workspace_relative_uses_posix_separators(tmp_path):
    workspace = tmp_path
    nested = tmp_path / "a" / "b.txt"

    assert workspace_relative(nested, workspace) == "a/b.txt"
