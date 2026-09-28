"""Browser login and streaming uploads for the HTTP workspace."""

from __future__ import annotations

import hmac
import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from .auth import presented_token
from .config import Settings
from .sandbox import GuardrailError, resolve_within

WEB_PREFIX = "/ui"
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PREVIEW_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
ASSETS = {
    "/ui/": ("index.html", "text/html; charset=utf-8"),
    "/ui/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/ui/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
ASSET_DIR = Path(__file__).with_name("web_assets")


def _json_error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class WebRouter:
    """Handle the browser UI while forwarding all other scopes to MCP."""

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(WEB_PREFIX):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = scope["path"]
        if path == WEB_PREFIX:
            response = Response(status_code=307, headers={"Location": "./ui/"})
        elif path in ASSETS and request.method in {"GET", "HEAD"}:
            filename, media_type = ASSETS[path]
            response = FileResponse(ASSET_DIR / filename, media_type=media_type)
        elif path == "/ui/api/session" and request.method == "GET":
            response = self._session(scope)
        elif path == "/ui/api/files" and request.method == "GET":
            response = self._files(scope)
        elif path == "/ui/api/file" and request.method == "GET":
            response = self._file(scope)
        elif path == "/ui/api/file" and request.method == "PUT":
            response = await self._save_file(request)
        elif path == "/ui/api/file" and request.method == "DELETE":
            response = self._delete_file(scope)
        elif path == "/ui/api/upload" and request.method == "POST":
            response = await self._upload(request)
        else:
            response = _json_error("Not found.", 404)

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' blob:; base-uri 'none'; frame-ancestors 'none'"
        )
        await response(scope, receive, send)

    def _authorized(self, scope) -> bool:
        expected = self.settings.auth_token
        actual = presented_token(scope)
        return bool(
            expected
            and actual
            and hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))
        )

    def _session(self, scope) -> Response:
        if self.settings.auth_token is None:
            return _json_error("Set PYEXEC_AUTH_TOKEN to use the web upload page.", 503)
        if not self._authorized(scope):
            return _json_error("Invalid token.", 401)
        return JSONResponse({"ok": True, "max_upload_bytes": MAX_UPLOAD_BYTES})

    def _files(self, scope) -> Response:
        if self.settings.auth_token is None:
            return _json_error("Set PYEXEC_AUTH_TOKEN to use the web upload page.", 503)
        if not self._authorized(scope):
            return _json_error("Invalid token.", 401)
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        relative = (query.get("path") or [""])[0]
        if relative.startswith("/") or "\\" in relative:
            return _json_error("Choose a relative path inside the workspace.", 400)
        try:
            directory = resolve_within(relative or ".", self.settings.workspace)
        except GuardrailError:
            return _json_error("That path is outside the workspace.", 403)
        if not directory.is_dir():
            return _json_error("Directory not found.", 404)
        try:
            entries = []
            for child in directory.iterdir():
                if child.is_symlink():
                    continue
                # Do not advertise symlinks into locations outside the workspace.
                try:
                    target = resolve_within(str(child), self.settings.workspace)
                except GuardrailError:
                    continue
                if not target.is_file() and not target.is_dir():
                    continue
                stat = target.stat()
                entries.append({
                    "name": child.name,
                    "path": child.relative_to(self.settings.workspace).as_posix(),
                    "is_dir": target.is_dir(),
                    "size_bytes": stat.st_size if target.is_file() else None,
                    "modified": stat.st_mtime,
                })
            entries.sort(key=lambda entry: (not entry["is_dir"], entry["name"].casefold()))
            return JSONResponse({"path": directory.relative_to(self.settings.workspace).as_posix() if directory != self.settings.workspace else "", "entries": entries})
        except OSError as exc:
            return _json_error(f"Could not list directory: {exc}", 400)

    def _file_target(self, scope) -> tuple[Path | None, Response | None]:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        relative = (query.get("path") or [""])[0]
        if not relative or relative.startswith("/") or "\\" in relative:
            return None, _json_error("Choose a relative file path inside the workspace.", 400)
        try:
            target = resolve_within(relative, self.settings.workspace)
        except GuardrailError:
            return None, _json_error("That path is outside the workspace.", 403)
        if target == self.settings.workspace:
            return None, _json_error("Choose a file or subdirectory.", 400)
        if target.relative_to(self.settings.workspace).as_posix() != relative:
            return None, _json_error("Use a canonical workspace path without symlinks.", 400)
        return target, None

    def _file(self, scope) -> Response:
        if not self._authorized(scope):
            return _json_error("Invalid token.", 401)
        target, error = self._file_target(scope)
        if error is not None:
            return error
        assert target is not None
        if not target.is_file():
            return _json_error("File not found.", 404)
        try:
            size = target.stat().st_size
            body = {
                "path": target.relative_to(self.settings.workspace).as_posix(),
                "size_bytes": size,
                "modified": target.stat().st_mtime,
                "kind": "other",
            }
            if target.suffix.lower() in IMAGE_TYPES and size <= MAX_IMAGE_PREVIEW_BYTES:
                body["kind"] = "image"
                body["mime_type"] = IMAGE_TYPES[target.suffix.lower()]
            elif size <= MAX_TEXT_BYTES:
                data = target.read_bytes()
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    if "\x00" not in content:
                        body["kind"] = "text"
                        body["content"] = content
                        body["etag"] = hashlib.sha256(data).hexdigest()
            return JSONResponse(body)
        except OSError as exc:
            return _json_error(f"Could not read file: {exc}", 400)

    async def _save_file(self, request: Request) -> Response:
        if not self._authorized(request.scope):
            return _json_error("Invalid token.", 401)
        target, error = self._file_target(request.scope)
        if error is not None:
            return error
        assert target is not None
        if not target.is_file():
            return _json_error("File not found.", 404)
        if target.stat().st_size > MAX_TEXT_BYTES:
            return _json_error("Only UTF-8 text files up to 2 MiB can be edited.", 415)
        try:
            previous = target.read_bytes()
            previous_text = previous.decode("utf-8")
            if "\x00" in previous_text:
                return _json_error("Only UTF-8 text files can be edited.", 415)
        except UnicodeDecodeError:
            return _json_error("Only UTF-8 text files can be edited.", 415)
        except OSError as exc:
            return _json_error(f"Could not read file: {exc}", 400)
        if request.headers.get("if-match") != hashlib.sha256(previous).hexdigest():
            return _json_error("File changed since it was opened. Reload before saving.", 409)

        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_TEXT_BYTES:
                return _json_error("Text exceeds the 2 MiB edit limit.", 413)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return _json_error("Content must be UTF-8 text.", 415)
        if "\x00" in content:
            return _json_error("Content must be UTF-8 text.", 415)

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".pyexec-edit-", delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
            if hashlib.sha256(target.read_bytes()).hexdigest() != request.headers.get("if-match"):
                return _json_error("File changed since it was opened. Reload before saving.", 409)
            os.replace(temporary, target)
            return JSONResponse({"path": target.relative_to(self.settings.workspace).as_posix(), "bytes_written": len(data), "etag": hashlib.sha256(data).hexdigest()})
        except OSError as exc:
            return _json_error(f"Could not save file: {exc}", 400)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _delete_file(self, scope) -> Response:
        if not self._authorized(scope):
            return _json_error("Invalid token.", 401)
        target, error = self._file_target(scope)
        if error is not None:
            return error
        assert target is not None
        if not target.exists():
            return _json_error("File or directory not found.", 404)
        try:
            is_dir = target.is_dir()
            if is_dir:
                target.rmdir()  # Never recursively delete a directory.
            else:
                target.unlink()
            return JSONResponse({"deleted": target.relative_to(self.settings.workspace).as_posix(), "is_dir": is_dir})
        except OSError:
            return _json_error("Could not delete. Directories must be empty and writable.", 409)

    async def _upload(self, request: Request) -> Response:
        if self.settings.auth_token is None:
            return _json_error("Set PYEXEC_AUTH_TOKEN to use the web upload page.", 503)
        if not self._authorized(request.scope):
            return _json_error("Invalid token.", 401)

        query = parse_qs(request.scope.get("query_string", b"").decode("latin-1"))
        relative = (query.get("path") or [""])[0]
        overwrite = (query.get("overwrite") or ["0"])[0] == "1"
        if not relative or relative.startswith("/") or "\\" in relative:
            return _json_error("Choose a relative path inside the workspace.", 400)
        try:
            target = resolve_within(relative, self.settings.workspace)
        except GuardrailError:
            return _json_error("That path is outside the workspace.", 403)
        if target == self.settings.workspace or target.is_dir():
            return _json_error("Choose a file path.", 400)
        if target.exists() and not overwrite:
            return _json_error("File already exists. Enable overwrite to replace it.", 409)

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_UPLOAD_BYTES:
                    return _json_error("File exceeds the 1 GiB upload limit.", 413)
            except ValueError:
                return _json_error("Invalid Content-Length.", 400)

        temporary: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Recheck after creating parents in case an existing component is a symlink.
            if resolve_within(relative, self.settings.workspace) != target:
                return _json_error("That path is outside the workspace.", 403)
            count = 0
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".pyexec-upload-", delete=False) as output:
                temporary = Path(output.name)
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > MAX_UPLOAD_BYTES:
                        return _json_error("File exceeds the 1 GiB upload limit.", 413)
                    output.write(chunk)

            if overwrite:
                os.replace(temporary, target)
            else:
                # A hard link publishes the complete temporary file without
                # replacing a file another request created in the meantime.
                os.link(temporary, target)
            return JSONResponse({"path": target.relative_to(self.settings.workspace).as_posix(), "bytes_written": count}, status_code=201)
        except FileExistsError:
            return _json_error("File already exists. Enable overwrite to replace it.", 409)
        except (OSError, GuardrailError) as exc:
            return _json_error(f"Upload failed: {exc}", 400)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


__all__ = ["WebRouter", "WEB_PREFIX", "MAX_UPLOAD_BYTES"]
