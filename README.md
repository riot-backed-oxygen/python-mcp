# pyexec-mcp

一个用 FastMCP 实现的 MCP server，让 LLM 能在这台机器上执行 Python 代码。

提供两种执行模型：**持久会话**（变量跨调用保留，像 notebook）和**一次性隔离进程**（每次全新解释器，互不污染），外加脚本运行、文件读写、依赖安装等配套工具。

---

## 关于 FastMCP 的一个前提

`mcp` 已经发布 2.x，**FastMCP 这个类被官方重命名为 `MCPServer`**，旧的导入路径已被删除：

```python
# mcp 1.x
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("demo")

# mcp 2.x —— 本项目的写法
from mcp.server import MCPServer
mcp = MCPServer("demo")
```

所以本项目用的就是 FastMCP（重命名后的官方实现），只是导入名变了。如果你手头的教程写的是 `mcp.server.fastmcp`，那它对应的是 `mcp<2`。本项目锁定 `mcp>=2.2.0`。

另一个容易踩的点：v2 里 transport 参数从构造函数挪到了 `run()`，且写法和 v1 不同——

```python
mcp.run(transport="streamable-http", host="127.0.0.1", port=8000)  # 连字符，不是下划线
```

---

## 安装

```bash
uv venv --python 3.10
uv pip install -e ".[dev]"
```

跑起来：

```bash
.venv/Scripts/python.exe -m pyexec_mcp                       # stdio（默认）
.venv/Scripts/python.exe -m pyexec_mcp --transport streamable-http --port 8000
```

注意 `--host` 的默认值是 `0.0.0.0`（监听所有网卡），而这是一个**能执行任意代码**的端点。所以有个联锁：非 loopback 地址上没配令牌就拒绝启动，见下面的「鉴权」。

## 接到 Claude Code

```bash
claude mcp add pyexec -- /绝对路径/python-mcp/.venv/Scripts/python.exe -m pyexec_mcp
```

或写进 MCP 配置：

```json
{
  "mcpServers": {
    "pyexec": {
      "command": "D:/develop/vibecoding/python-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "pyexec_mcp"],
      "env": {
        "PYEXEC_WORKSPACE": "D:/develop/vibecoding/python-mcp"
      }
    }
  }
}
```

---

## 工具

| 工具 | 用途 |
|---|---|
| `pyexec_run` | 在持久会话里执行代码，变量跨调用保留 |
| `pyexec_run_isolated` | 在全新解释器里执行，跑完即弃 |
| `pyexec_run_script` | 以 `__main__` 方式运行磁盘上的 .py 文件 |
| `pyexec_reset_session` | 重启会话，清空所有变量和 import |
| `pyexec_session_info` | 查看会话里已定义了哪些变量、用的哪个解释器 |
| `pyexec_read_file` | 读取工作区内的文本文件 |
| `pyexec_write_file` | 写入工作区内的文件 |
| `pyexec_list_files` | 列出工作区文件，支持 glob 和递归 |
| `pyexec_list_packages` | 列出可 import 的已安装包 |
| `pyexec_install_package` | 安装依赖到执行代码所用的解释器 |

配套的 MCP resource：`pyexec://config` 返回当前生效的配置。

### 什么时候用哪个

```
需要上一步的变量？        → pyexec_run
需要干净环境 / 可能崩？    → pyexec_run_isolated
跑的是已存在的文件？       → pyexec_run_script
会话里状态乱了想重来？     → pyexec_reset_session
```

---

## 配置

全部通过环境变量，CLI flag 会覆盖同名环境变量。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PYEXEC_WORKSPACE` | 进程 cwd | 文件工具的根目录，也是 worker 的工作目录 |
| `PYEXEC_PYTHON` | 服务器自身的解释器 | 执行代码用的解释器 |
| `PYEXEC_TIMEOUT` | `30` | 单次调用的默认超时（秒） |
| `PYEXEC_MAX_TIMEOUT` | `300` | 调用方传入超时的上限 |
| `PYEXEC_MAX_OUTPUT_CHARS` | `40000` | 每个输出流保留的字符数，超出截断 |
| `PYEXEC_ALLOWED_MODULES` | 未设置 | 允许 import 的顶层模块白名单，逗号分隔 |
| `PYEXEC_INHERIT_ENV` | `1` | 是否让 worker 继承服务器环境变量 |
| `PYEXEC_ENV_FILE` | `.env` | 启动时合并这个文件里的变量 |
| `PYEXEC_AUTH_TOKEN` | 未设置 | streamable-http 的 Bearer 令牌，至少 16 字符 |
| `PYEXEC_AUTH_TOKEN_FILE` | 未设置 | 从文件读令牌；`PYEXEC_AUTH_TOKEN` 优先 |

### 用 `.env` 配

Python 不会自己读 `.env`，所以启动时 server 会主动合并工作目录下的 `.env`（或者 `PYEXEC_ENV_FILE` 指向的文件）：

```bash
# .env
PYEXEC_AUTH_TOKEN=<python -c "import secrets; print(secrets.token_urlsafe(32))">
PYEXEC_WORKSPACE=D:/develop/vibecoding/python-mcp
PYEXEC_TIMEOUT=30
```

支持 `#` 注释、空行、可选的 `export ` 前缀、单/双引号包裹、双引号内的反斜杠转义，以及不带引号时行尾的注释（` #` 起头）。UTF-8 BOM 会被去掉（记事本存出来的文件能用）。

几条刻意的规则：

- **真实环境变量永远赢**。已经 `export` 的值不会被 `.env` 覆盖，命令行 flag 又覆盖环境变量。优先级是 `flag > export > .env`，所以不会出现"明明 export 了却读到文件里旧值"这种事。
- **`.env` 读不到就静默跳过**，不算错误——没这个文件是完全正常的。但 `PYEXEC_ENV_FILE` 指向一个存在却读不了的文件会明确报错。

- **加载发生在解析 flag 之前**，所以 `.env` 里的 `PYEXEC_HOST`/`PYEXEC_PORT`/`PYEXEC_TRANSPORT` 能当默认值用。
- 日志只会打印**加载了哪些变量名**，不打印值——令牌不会进日志。


`PYEXEC_ALLOWED_MODULES` 值得单独说一句：不设置时完全不限制 import；一旦设置，就只有白名单内的模块能被导入，检查在建进程之前用 AST 完成：

```bash
PYEXEC_ALLOWED_MODULES=math,json,statistics
```

---

## 鉴权（只对 streamable-http 生效）

stdio 不需要鉴权——它的访问控制就是"能启动这个进程"。streamable-http 监听端口，必须自己把关。

**用法**：配一个至少 16 字符的共享密钥，客户端在 `Authorization` 头里带上它。

```bash
# 环境变量（推荐，密钥不进进程列表）
export PYEXEC_AUTH_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
.venv/Scripts/python.exe -m pyexec_mcp --transport streamable-http --host 0.0.0.0 --port 8000

# 或者从文件读
.venv/Scripts/python.exe -m pyexec_mcp --transport streamable-http --auth-token-file ./token.txt
```

也可以直接写进 `.env`（见上面「用 `.env` 配」），server 启动时会自动加载。

客户端配置：

```json
{
  "mcpServers": {
    "pyexec": {
      "type": "http",
      "url": "http://192.168.1.10:8000/mcp",
      "headers": { "Authorization": "Bearer <你的令牌>" }
    }
  }
}
```

**启动联锁**：`--host` 不是 loopback（`127.0.0.1`/`::1`/`localhost`）而 `PYEXEC_AUTH_TOKEN` 又没配，进程直接退出并说明原因。这是故意的——`--host` 的默认值就是 `0.0.0.0`，而这个端点能执行任意代码，不能因为忘了配就裸奔到局域网上。想无鉴权只在本地用，就 `--host 127.0.0.1`。

**实现上的几个选择**：

- **不用 SDK 的 `TokenVerifier` + `AuthSettings`**。那套是 OAuth 形状的，`AuthSettings` 强制要求 `issuer_url`；对一个自签的静态密钥来说，那等于编造一个发现端点让客户端去访问，只会更绕。这里用的是纯 ASGI 中间件（`auth.py`），没有额外依赖。
- **令牌只从 `Authorization` 头读，不认 query string**。URL 里的密钥会漏进 access log、`Referer`、浏览器历史。
- **比较用 `hmac.compare_digest`**，常数时间，不给时序侧信道。
- **401 区分两种情况**：没带头返回 `error="invalid_request"`，带了但不匹配返回 `error="invalid_token"`，客户端能分清是"忘了配"还是"配错了"。
- **非 HTTP scope（比如 lifespan）直接放行**，否则启动握手自己就被拦了。

令牌泄露等于对方能在这台机器上以 server 进程的权限执行任意代码，跟直接交出 shell 是一回事——别再拿去当 API key 到处贴。

---

## 安全边界

**这不是沙箱，请当它没有隔离来用。** 这个 server 的全部意义就是执行任意 Python，而任意 Python 可以绕过任何运行在同一解释器内的检查——`PYEXEC_ALLOWED_MODULES` 挡得住手滑，挡不住存心。代码跑起来后，它对这台机器的权限和 server 进程本身完全一样。

代码里真正提供的是这些**护栏**：

- **AST 预检**：import 白名单（可选）、语法错误在建进程前就报出来
- **路径囚笼**：文件工具的路径必须解析在工作区之内，`../` 逃逸会被拒绝
- **双层超时**：worker 自己用软中断（`PyThreadState_SetAsyncExc`）在字节码边界抛 `TimeoutError`，保留会话；卡在 C 调用里软中断够不着时，父进程到点硬杀
- **包名校验**：`pyexec_install_package` 拒绝形如 `--target` 的参数，避免被当成 pip flag
- **输出截断**：防止一段死循环 print 撑爆上下文
- **`PYEXEC_INHERIT_ENV=0`**：worker 只拿到 `PATH`/`TEMP` 等启动所需变量，API key 之类的凭据不外泄
- **Bearer 鉴权 + 启动联锁**：streamable-http 在非 loopback 地址上没配令牌就拒绝启动

真要隔离，请用容器、虚拟机或受限用户账户——把整个 server 关在里面，而不是指望这一层。

---

## 架构

```
MCP client
    │  stdio / streamable-http
    ▼
server.py          MCPServer + 10 个工具，把 Settings 和 Session 串起来
    │
    ├── auth.py       streamable-http 的 Bearer 中间件 + 启动联锁
    ├── config.py     Settings：环境变量 → 配置，含令牌校验
    ├── dotenv.py     极简 .env 读取（不引 python-dotenv）
    ├── sandbox.py     AST import 白名单、路径囚笼（进 worker 之前）
    ├── packages.py    pip 安装 / 列举
    │
    ▼  一行一个 JSON，stdout 上带 nonce 的帧
session.py         每个会话一个 worker 子进程，串行化调用，超时硬杀
    │
    ▼
worker.py          独立的子进程：ast 解析 → exec → 捕获输出 → 回帧
```

几个设计取舍：

**为什么 worker 是独立文件、不 import 本包？** 这样父进程可以用绝对路径直接拉起它，不管包有没有被安装、装在哪，都不会因为 import 路径问题起不来。

**为什么响应帧要带 nonce？** 帧就是 stdout 上的一行文本，而执行的代码也会往 stdout 写。随机 nonce 让代码没法伪造一行"执行成功"的响应骗过父进程。

**为什么 print 的输出在帧里、而不是直接进管道？** worker 执行期间把 `sys.stdout` 换成 `StringIO`，所以 `print` 的内容随帧一起回来，结构清晰。直接写文件描述符 1 的输出（C 扩展、子进程、`os.write`）绕不过这个替换，会作为 `raw_output` 单独返回，不丢。

**为什么超时要杀进程而不是中断调用？** 软中断对纯 Python 循环有效，但挡不住阻塞在 C 调用里的代码。与其留一个还在改命名空间的线程，不如杀掉——下次调用会自动起一个新的，代价是会话里之前的变量没了，这一点在返回的 `session_restarted` 里有明确标记。

---

## 测试

```bash
.venv/Scripts/python.exe -m pytest -v
```

---

## 已知限制

- 一次只能有一个调用在跑。会话是有状态的，并发执行会让命名空间错乱。第二个调用等 5 秒拿不到锁会直接返回 `SessionBusy`，提示改用 `pyexec_run_isolated`。
- 超时后的行为分两种情况，返回值里的 `session_restarted` 会告诉你是哪种：纯 Python 循环会被就地打断，**会话和变量都保留**；卡在阻塞式 C 调用里的代码打断不了，只能杀掉进程，**会话被重置、之前的变量丢失**。别假设，看这个字段。
- `pyexec_install_package` 装进的是 `PYEXEC_PYTHON` 指向的解释器。如果用 `uvx` 之类启动 server，那可能是个临时环境，装完就没了——这种情况建议显式设置 `PYEXEC_PYTHON`。命令会优先用 `uv pip install`（因为 `uv venv` 建的环境里根本没有 pip）。
- Windows 上 worker 的输出统一按 UTF-8 处理（子进程强制 `PYTHONIOENCODING=utf-8`），避免中文 locale 下 cp936 把帧搅乱。
