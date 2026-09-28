(() => {
  "use strict";
  const key = "pyexec.workspace.token";
  const $ = (id) => document.getElementById(id);
  let currentDirectory = "";
  let currentFile = null;
  let currentEtag = null;
  let imageUrl = null;
  let busy = false;

  function tokenHeader() {
    return { Authorization: `Bearer ${sessionStorage.getItem(key)}` };
  }

  function setMessage(id, text, kind = "") {
    const node = $(id);
    node.textContent = text;
    node.className = `message ${kind}`;
  }

  function sizeOf(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function setAuthenticated(value) {
    $("login-view").classList.toggle("hidden", value);
    $("upload-view").classList.toggle("hidden", !value);
    $("step-label").textContent = value ? "02 / 文件管理" : "01 / 验证身份";
    $("header-status").textContent = value ? "已连接" : "未连接";
    $("logout").classList.toggle("hidden", !value);
    document.querySelector(".status-dot").classList.toggle("online", value);
    if (value) loadFiles("");
  }

  async function responseBody(response) {
    return response.json().catch(() => ({}));
  }

  async function checkToken(token) {
    const response = await fetch("./api/session", {
      headers: { Authorization: `Bearer ${token}` }, cache: "no-store"
    });
    if (!response.ok) {
      const body = await responseBody(response);
      throw new Error(response.status === 401 ? "令牌不正确，请重试。" : (body.error || "无法连接到服务。"));
    }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = $("token").value.trim();
    if (!token) return;
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    setMessage("login-message", "正在验证…");
    try {
      await checkToken(token);
      sessionStorage.setItem(key, token);
      $("token").value = "";
      setMessage("login-message", "");
      setAuthenticated(true);
    } catch (error) {
      setMessage("login-message", error.message || "网络连接失败。", "error");
    } finally {
      button.disabled = false;
    }
  });

  $("toggle-token").addEventListener("click", () => {
    const input = $("token");
    const visible = input.type === "password";
    input.type = visible ? "text" : "password";
    $("toggle-token").textContent = visible ? "隐藏" : "显示";
    $("toggle-token").setAttribute("aria-label", visible ? "隐藏令牌" : "显示令牌");
  });

  function clearInspector() {
    currentFile = null;
    currentEtag = null;
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
    $("inspector-content").classList.add("hidden");
    $("file-dialog-title").textContent = "查看文件";
    $("upload-view").classList.remove("inspector-open");
    document.querySelector(".workspace-inspector").classList.add("hidden");
  }

  $("close-inspector").addEventListener("click", clearInspector);

  $("logout").addEventListener("click", () => {
    sessionStorage.removeItem(key);
    $("file-list").replaceChildren();
    clearInspector();
    setMessage("files-message", "");
    setMessage("upload-message", "");
    setAuthenticated(false);
  });

  function action(label, handler, className = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.className = className;
    button.addEventListener("click", handler);
    return button;
  }

  function renderFiles(entries) {
    const list = $("file-list");
    list.replaceChildren();
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "empty-list";
      empty.textContent = "此目录暂无文件";
      list.append(empty);
      return;
    }
    for (const entry of entries) {
      const row = document.createElement("div");
      row.className = "file-row";
      const kind = document.createElement("span");
      kind.className = "file-kind";
      kind.textContent = entry.is_dir ? "▣" : "◫";
      const title = document.createElement("span");
      title.className = "file-title";
      title.title = entry.name;
      title.textContent = entry.name;
      const size = document.createElement("span");
      size.className = "file-size";
      size.textContent = entry.is_dir ? "目录" : sizeOf(entry.size_bytes);
      const actions = document.createElement("div");
      actions.className = "file-actions";
      if (entry.is_dir) {
        actions.append(action("打开", () => loadFiles(entry.path)));
      } else {
        actions.append(action("查看", () => openFile(entry)));
        actions.append(action("下载", () => downloadFile(entry)));
      }
      actions.append(action("删除", () => deleteEntry(entry), "delete-action"));
      row.append(kind, title, size, actions);
      list.append(row);
    }
  }

  async function loadFiles(path) {
    setMessage("files-message", "正在读取目录…");
    try {
      const response = await fetch(`./api/files?${new URLSearchParams({ path })}`, {
        headers: tokenHeader(), cache: "no-store"
      });
      const body = await responseBody(response);
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      if (body.path !== currentDirectory) clearInspector();
      currentDirectory = body.path;
      $("current-path").textContent = `/${currentDirectory}`;
      $("parent-folder").disabled = !currentDirectory;
      renderFiles(body.entries);
      setMessage("files-message", `${body.entries.length} 项`);
    } catch (error) {
      setMessage("files-message", error.message || "读取目录失败。", "error");
    }
  }

  $("refresh-files").addEventListener("click", () => loadFiles(currentDirectory));
  $("parent-folder").addEventListener("click", () => loadFiles(currentDirectory.split("/").slice(0, -1).join("/")));

  async function uploadFiles(selected) {
    if (busy || !selected.length) return;
    busy = true;
    $("upload-button").disabled = true;
    const directory = currentDirectory;
    let completed = 0;
    for (const file of selected) {
      const path = directory ? `${directory}/${file.name}` : file.name;
      setMessage("upload-message", `正在上传 ${file.name}…`);
      try {
        let response = await fetch(`./api/upload?${new URLSearchParams({ path })}`, {
          method: "POST", headers: { ...tokenHeader(), "Content-Type": "application/octet-stream" }, body: file
        });
        if (response.status === 409 && window.confirm(`${path} 已存在，是否覆盖？`)) {
          response = await fetch(`./api/upload?${new URLSearchParams({ path, overwrite: "1" })}`, {
            method: "POST", headers: { ...tokenHeader(), "Content-Type": "application/octet-stream" }, body: file
          });
        }
        const body = await responseBody(response);
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        completed++;
      } catch (error) {
        setMessage("upload-message", `${file.name}: ${error.message || "上传失败"}`, "error");
      }
    }
    busy = false;
    $("upload-button").disabled = false;
    if (completed === selected.length) setMessage("upload-message", `${completed} 个文件已上传到当前目录。`, "success");
    loadFiles(currentDirectory);
  }

  $("upload-button").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (event) => {
    const selected = [...event.target.files];
    event.target.value = "";
    uploadFiles(selected);
  });

  function downloadUrl(path) {
    return `../download/${path.split("/").map(encodeURIComponent).join("/")}`;
  }

  async function downloadFile(entry) {
    setMessage("files-message", `正在下载 ${entry.name}…`);
    try {
      const response = await fetch(downloadUrl(entry.path), { headers: tokenHeader() });
      if (!response.ok) throw new Error(`下载失败 (HTTP ${response.status})`);
      const objectUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = entry.name;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
      setMessage("files-message", `${entry.name} 已开始下载。`, "success");
    } catch (error) {
      setMessage("files-message", error.message || "下载失败。", "error");
    }
  }

  async function openFile(entry) {
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
    currentFile = entry;
    currentEtag = null;
    $("upload-view").classList.add("inspector-open");
    document.querySelector(".workspace-inspector").classList.remove("hidden");
    $("inspector-content").classList.remove("hidden");
    $("file-dialog-title").textContent = entry.name;
    $("file-dialog-path").textContent = entry.path;
    $("file-dialog-size").textContent = sizeOf(entry.size_bytes);
    $("file-content").classList.add("hidden");
    $("image-preview").classList.add("hidden");
    $("preview-note").classList.add("hidden");
    $("edit-file").classList.add("hidden");
    $("save-file").classList.add("hidden");
    setMessage("file-dialog-message", "正在读取文件…");
    try {
      const response = await fetch(`./api/file?${new URLSearchParams({ path: entry.path })}`, {
        headers: tokenHeader(), cache: "no-store"
      });
      const body = await responseBody(response);
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      if (currentFile !== entry) return;
      if (body.kind === "text") {
        currentEtag = body.etag;
        $("file-content").value = body.content;
        $("file-content").readOnly = true;
        $("file-content").classList.remove("hidden");
        $("edit-file").classList.remove("hidden");
      } else if (body.kind === "image") {
        const imageResponse = await fetch(downloadUrl(entry.path), { headers: tokenHeader() });
        if (!imageResponse.ok) throw new Error(`预览失败 (HTTP ${imageResponse.status})`);
        const blob = await imageResponse.blob();
        if (currentFile !== entry) return;
        imageUrl = URL.createObjectURL(blob);
        $("image-preview").src = imageUrl;
        $("image-preview").classList.remove("hidden");
      } else {
        $("preview-note").textContent = "此文件无法在线预览或超过预览大小限制。你仍可下载查看。";
        $("preview-note").classList.remove("hidden");
      }
      setMessage("file-dialog-message", "");
    } catch (error) {
      if (currentFile === entry) setMessage("file-dialog-message", error.message || "读取文件失败。", "error");
    }
  }

  $("edit-file").addEventListener("click", () => {
    $("file-content").readOnly = false;
    $("file-content").focus();
    $("edit-file").classList.add("hidden");
    $("save-file").classList.remove("hidden");
    setMessage("file-dialog-message", "编辑 UTF-8 文本，完成后点击保存。");
  });

  $("save-file").addEventListener("click", async () => {
    if (!currentFile || !currentEtag) return;
    const button = $("save-file");
    const entry = currentFile;
    const etag = currentEtag;
    const content = $("file-content").value;
    button.disabled = true;
    try {
      const response = await fetch(`./api/file?${new URLSearchParams({ path: entry.path })}`, {
        method: "PUT", headers: { ...tokenHeader(), "If-Match": etag, "Content-Type": "text/plain; charset=utf-8" },
        body: content
      });
      const body = await responseBody(response);
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      if (currentFile !== entry) { loadFiles(currentDirectory); return; }
      currentEtag = body.etag;
      entry.size_bytes = body.bytes_written;
      $("file-dialog-size").textContent = sizeOf(body.bytes_written);
      $("file-content").readOnly = true;
      $("edit-file").classList.remove("hidden");
      button.classList.add("hidden");
      setMessage("file-dialog-message", "已保存。", "success");
      loadFiles(currentDirectory);
    } catch (error) {
      if (currentFile === entry) setMessage("file-dialog-message", error.message || "保存失败。", "error");
    } finally {
      button.disabled = false;
    }
  });

  $("download-current").addEventListener("click", () => { if (currentFile) downloadFile(currentFile); });

  async function deleteEntry(entry) {
    const detail = entry.is_dir ? "只能删除空目录。" : "此操作无法撤销。";
    if (!window.confirm(`确定删除 ${entry.path}？\n${detail}`)) return;
    try {
      const response = await fetch(`./api/file?${new URLSearchParams({ path: entry.path })}`, {
        method: "DELETE", headers: tokenHeader()
      });
      const body = await responseBody(response);
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      if (currentFile && currentFile.path === entry.path) clearInspector();
      await loadFiles(currentDirectory);
      setMessage("files-message", `已删除 ${entry.path}。`, "success");
    } catch (error) {
      setMessage("files-message", error.message || "删除失败。", "error");
    }
  }

  const saved = sessionStorage.getItem(key);
  if (saved) checkToken(saved).then(() => setAuthenticated(true)).catch(() => {
    sessionStorage.removeItem(key);
    setAuthenticated(false);
  });
})();
