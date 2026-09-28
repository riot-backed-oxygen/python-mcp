"""Browser workspace routes over a running HTTP server."""

from __future__ import annotations

import httpx


def test_web_login_upload_browse_and_download(live_server):
    headers = {"Authorization": f"Bearer {live_server.token}"}
    with httpx.Client(base_url=live_server.base_url, trust_env=False, timeout=10) as client:
        page = client.get("/ui/")
        assert page.status_code == 200
        assert "PYEXEC_AUTH_TOKEN" in page.text
        assert client.get("/ui/app.js").status_code == 200

        assert client.get("/ui/api/session").status_code == 401
        assert client.get("/ui/api/session", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/ui/api/session", headers=headers).json()["ok"] is True
        assert client.get("/ui/api/files").status_code == 401

        data = b"\x00\xff\x10a,b\n"
        upload = client.post("/ui/api/upload", params={"path": "data/input.bin"}, headers=headers, content=data)
        assert upload.status_code == 201
        assert upload.json() == {"path": "data/input.bin", "bytes_written": len(data)}
        assert (live_server.workspace / "data" / "input.bin").read_bytes() == data

        root = client.get("/ui/api/files", headers=headers).json()
        assert any(entry["name"] == "data" and entry["is_dir"] for entry in root["entries"])
        folder = client.get("/ui/api/files", params={"path": "data"}, headers=headers).json()
        assert folder["entries"][0]["name"] == "input.bin"
        assert folder["entries"][0]["size_bytes"] == len(data)
        download = client.get("/download/data/input.bin", headers=headers)
        assert download.status_code == 200
        assert download.content == data

        conflict = client.post("/ui/api/upload", params={"path": "data/input.bin"}, headers=headers, content=b"new")
        assert conflict.status_code == 409
        assert (live_server.workspace / "data" / "input.bin").read_bytes() == data
        replaced = client.post("/ui/api/upload", params={"path": "data/input.bin", "overwrite": "1"}, headers=headers, content=b"new")
        assert replaced.status_code == 201
        assert (live_server.workspace / "data" / "input.bin").read_bytes() == b"new"


def test_web_upload_rejects_unauthorized_and_outside_paths(live_server):
    headers = {"Authorization": f"Bearer {live_server.token}"}
    outside = f"{live_server.workspace.name}-web-outside.txt"
    with httpx.Client(base_url=live_server.base_url, trust_env=False, timeout=10) as client:
        assert client.post("/ui/api/upload", params={"path": "secret.txt"}, content=b"x").status_code == 401
        assert client.post("/ui/api/upload", params={"path": f"../{outside}"}, headers=headers, content=b"x").status_code == 403
        assert client.get("/ui/api/files", params={"path": ".."}, headers=headers).status_code == 403
    assert not (live_server.workspace.parent / outside).exists()


def test_web_preview_edit_and_delete(live_server):
    headers = {"Authorization": f"Bearer {live_server.token}"}
    note = live_server.workspace / "note.txt"
    note.write_bytes(b"before\n")
    empty = live_server.workspace / "empty"
    empty.mkdir()
    occupied = live_server.workspace / "occupied"
    occupied.mkdir()
    (occupied / "child.txt").write_text("keep", encoding="utf-8")
    binary = live_server.workspace / "raw.bin"
    binary.write_bytes(b"\x00\xff")

    with httpx.Client(base_url=live_server.base_url, trust_env=False, timeout=10) as client:
        assert client.get("/ui/api/file", params={"path": "note.txt"}).status_code == 401
        preview = client.get("/ui/api/file", params={"path": "note.txt"}, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["kind"] == "text"
        assert preview.json()["content"] == "before\n"
        version = preview.json()["etag"]

        saved = client.put("/ui/api/file", params={"path": "note.txt"}, headers={**headers, "If-Match": version}, content="after\n".encode())
        assert saved.status_code == 200
        assert note.read_bytes() == b"after\n"
        stale = client.put("/ui/api/file", params={"path": "note.txt"}, headers={**headers, "If-Match": version}, content=b"lost")
        assert stale.status_code == 409
        assert note.read_bytes() == b"after\n"

        assert client.get("/ui/api/file", params={"path": "raw.bin"}, headers=headers).json()["kind"] == "other"
        assert client.put("/ui/api/file", params={"path": "raw.bin"}, headers={**headers, "If-Match": "x"}, content=b"text").status_code == 415
        assert client.delete("/ui/api/file", params={"path": ".."}, headers=headers).status_code == 403
        assert client.delete("/ui/api/file", params={"path": "occupied"}, headers=headers).status_code == 409
        assert client.delete("/ui/api/file", params={"path": "empty"}, headers=headers).status_code == 200
        assert client.delete("/ui/api/file", params={"path": "note.txt"}, headers=headers).status_code == 200

    assert not note.exists()
    assert not empty.exists()
    assert (occupied / "child.txt").exists()
