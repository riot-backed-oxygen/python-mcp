# pyexec-mcp

一个用 FastMCP 实现的 MCP server，让 LLM 能在这台机器上执行 Python 代码。

提供三种执行模型：**持久会话**（变量跨调用保留，像 notebook）、**一次性隔离进程**（每次全新解释器，互不污染）和**后台任务**（下达即返回，跑完自动通知），外加脚本运行、文件读写、依赖安装等配套工具。

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
| `pyexec_run_background` | 把代码丢到后台跑，立刻返回 job id |
| `pyexec_run_script_background` | 把磁盘上的 .py 丢到后台跑，立刻返回 job id |
| `pyexec_job_status` | 查一个后台任务的状态；不带参数则列出全部 |
| `pyexec_job_result` | 取后台任务的完整 stdout/stderr/返回值 |
| `pyexec_job_cancel` | 杀掉在跑的后台任务 |
| `pyexec_reset_session` | 重启会话，清空所有变量和 import |
| `pyexec_session_info` | 查看会话里已定义了哪些变量、用的哪个解释器 |
| `pyexec_read_file` | 读取工作区内的文本文件 |
| `pyexec_write_file` | 写入工作区内的文件 |
| `pyexec_list_files` | 列出工作区文件，支持 glob 和递归 |
| `pyexec_get_download_url` | 生成一个带签名的限时下载地址，浏览器直接点开 |
| `pyexec_notify` | 往 Telegram 发一条消息（配了才可用） |
| `pyexec_list_packages` | 列出可 import 的已安装包 |
| `pyexec_install_package` | 安装依赖到执行代码所用的解释器 |

配套的 MCP resource：`pyexec://config` 返回当前生效的配置。

### 什么时候用哪个

```
需要上一步的变量？        → pyexec_run
需要干净环境 / 可能崩？    → pyexec_run_isolated
跑的是已存在的文件？       → pyexec_run_script
跑得久、不想干等？         → pyexec_run_background / pyexec_run_script_background
要看后台任务跑完没有？     → pyexec_job_status
要把生成的文件给人？       → pyexec_get_download_url
跑得久想被通知一声？       → pyexec_notify（够久的运行会自动通知，见「Telegram 通知」）
会话里状态乱了想重来？     → pyexec_reset_session
```

判断标准是**下一步能不能等**：跑完还要接着用结果，就老老实实用阻塞版；跑完这一步就结束了，才用后台版。后台任务的返回值只有一个 job id，拿不到结果本身。

---

## 配置

全部通过环境变量，CLI flag 会覆盖同名环境变量。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PYEXEC_WORKSPACE` | 进程 cwd | 文件工具的根目录，也是 worker 的工作目录 |
| `PYEXEC_PYTHON` | 服务器自身的解释器 | 执行代码用的解释器 |
| `PYEXEC_TIMEOUT` | `30` | 单次调用的默认超时（秒） |
| `PYEXEC_MAX_TIMEOUT` | `300` | 阻塞式调用传入超时的上限；不限制后台任务 |
| `PYEXEC_MAX_OUTPUT_CHARS` | `40000` | 每个输出流保留的字符数，超出截断 |
| `PYEXEC_BACKGROUND_TIMEOUT` | `3600` | 后台任务没传 timeout 时用的默认超时（秒） |
| `PYEXEC_BACKGROUND_MAX_TIMEOUT` | `86400` | 后台任务 timeout 的上限（秒） |
| `PYEXEC_MAX_BACKGROUND_JOBS` | `4` | 同时最多跑几个后台任务，超了直接拒绝 |
| `PYEXEC_JOB_HISTORY` | `20` | 已完成的后台任务保留几条（只在内存里） |
| `PYEXEC_ALLOWED_MODULES` | 未设置 | 允许 import 的顶层模块白名单，逗号分隔 |
| `PYEXEC_INHERIT_ENV` | `1` | 是否让 worker 继承服务器环境变量 |
| `PYEXEC_ENV_FILE` | `.env` | 启动时合并这个文件里的变量 |
| `PYEXEC_BASE_URL` | 由 `--host`/`--port` 推导 | 客户端访问本服务的地址，用于拼下载链接 |
| `PYEXEC_DOWNLOAD_TTL` | `3600` | 下载链接有效期（秒），也是单次请求能要到的上限 |
| `PYEXEC_AUTH_TOKEN` | 未设置 | streamable-http 的 Bearer 令牌，至少 16 字符 |
| `PYEXEC_AUTH_TOKEN_FILE` | 未设置 | 从文件读令牌；`PYEXEC_AUTH_TOKEN` 优先 |
| `PYEXEC_TELEGRAM_BOT_TOKEN` | 未设置 | Telegram 机器人令牌，`@BotFather` 给的那个 |
| `PYEXEC_TELEGRAM_BOT_TOKEN_FILE` | 未设置 | 从文件读机器人令牌；上面的变量优先 |
| `PYEXEC_TELEGRAM_CHAT_ID` | 未设置 | 消息发到哪个会话；和令牌**两个都配**才启用 |
| `PYEXEC_TELEGRAM_MIN_DURATION` | `60` | 墙钟耗时达到这个秒数的运行，结束后自动通知；`0` 表示每次都通知 |
| `PYEXEC_TELEGRAM_PROXY` | 跟随环境变量 | 访问 `api.telegram.org` 用的代理；设成空值表示直连 |

### 用 `.env` 配

Python 不会自己读 `.env`，所以启动时 server 会主动合并工作目录下的 `.env`（或者 `PYEXEC_ENV_FILE` 指向的文件）：

```bash
# .env
PYEXEC_AUTH_TOKEN=<python -c "import secrets; print(secrets.token_urlsafe(32))">
PYEXEC_WORKSPACE=D:/develop/vibecoding/python-mcp
PYEXEC_BASE_URL=http://192.168.1.10:8000
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

## 文件下载（只对 streamable-http 生效）

代码生成的东西——图表、报表、导出的 csv——没地方去：`pyexec_read_file` 只能把内容读回模型上下文，二进制读不了，大文件还会挤爆上下文。所以配一个 HTTP 下载接口：

```
GET <PYEXEC_BASE_URL>/download/<工作区内的相对路径>?exp=<unix 秒>&sig=<hmac>
```

`pyexec_get_download_url` 就是用来生成这个地址的，拿到后直接丢给用户点：

```python
# 在会话里画完图之后
pyexec_get_download_url("chart.png")
# -> http://192.168.1.10:8000/download/chart.png?exp=1759046400&sig=9f2c...
```

`--base-url` / `PYEXEC_BASE_URL` 决定链接的前缀。默认按 `--host`/`--port` 推导，但**只有你知道客户端到底怎么访问这台机器**——中间可能有 NAT、端口转发、反向代理，都不体现在绑定地址上，所以这几种情况要显式配。绑 `0.0.0.0` 时推导出来的是本机出口网卡地址（拿不到就回落 `127.0.0.1`），因为「监听所有网卡」本身不是一个能拨号的地址。值里可以带路径前缀（`https://host/pyexec`），要求反代把前缀剥掉再转发。

**为什么是签名，而不是让下载也带 Bearer 头？** 因为链接的全部意义是人能在浏览器里点开，而浏览器导航带不上 `Authorization` 头。把令牌塞进 query string 又违反上面鉴权那一节的原则——URL 会漏进 access log、`Referer`、浏览器历史。签名是第三个选项：密钥派生自 `PYEXEC_AUTH_TOKEN`（URL 里没有令牌本身），覆盖「这一个路径 + 这一个到期时间」，所以

- 改路径、改到期时间、换个令牌签，都直接 401
- 链接泄露只暴露**一个文件**、且**有时限**，而不是整个工作区的永久通行证
- 换掉 `PYEXEC_AUTH_TOKEN` 就等于作废所有已发出的链接

签名是对**解析后的工作区相对路径**做的，不是 URL 里那串原始文本，所以 `%2e%2e%2f`、重复斜杠之类的写法不影响校验；路径囚笼仍然是 `resolve_within`，越界一律 403。

**没配令牌时下载接口不要求签名。** 这不是漏的：启动联锁保证「无令牌 ⇒ 只能绑 loopback」，此时能访问这个端口的本来就只有本机，和 MCP 端点在 loopback 上不鉴权是同一个道理。带上正确的 Bearer 头也能下载，方便 curl 和脚本。

有效期默认 1 小时，`PYEXEC_DOWNLOAD_TTL` 改。这个是**上限**不是默认值：单次调用可以要短一点，要不了更长——能决定「链接泄露后危险多久」的应该是运维配置，不是某一次工具调用。

```bash
# 本机 HTTP_PROXY 存在时记得 --noproxy，否则请求会被代理截走
curl -sS --noproxy '*' -O -J "http://127.0.0.1:8000/download/chart.png?exp=...&sig=..."
```

实现上还有两点：路由用的是**纯 ASGI 前置门**（`downloads.py`），不是 Starlette 的 `Mount`——`Mount` 不会把 lifespan 转发给子应用，挂上去 streamable-http 的 session manager 就起不来，每个请求都会以 `RuntimeError: Task group is not initialized` 失败。它也**故意放在 Bearer 中间件之外**，自己校验签名，否则那道门拦下的正好是它要服务的请求。文件响应复用 Starlette 的 `FileResponse`，因此自带流式发送、`Range`（下载可断点续传）、`ETag`，并按扩展名给出 `Content-Type`。

---

## Telegram 通知（可选）

长时间脚本的痛点不是慢，是**没法盯着**：工具调用要等代码跑完才返回，十分钟的脚本就是十分钟的信息真空。配一个 bot，跑得够久的任务结束时给自己发一条消息，带上结果和输出的结尾。

```bash
# .env
PYEXEC_TELEGRAM_BOT_TOKEN=123456789:AA...     # @BotFather 创建 bot 时给的
PYEXEC_TELEGRAM_CHAT_ID=-1001234567890
PYEXEC_TELEGRAM_MIN_DURATION=60               # 跑够 60 秒才通知，0 表示每次都通知
PYEXEC_TELEGRAM_PROXY=http://127.0.0.1:7890   # 连不上 api.telegram.org 时用
```

**chat id 从哪来**：bot 自己不知道要发给谁。先给 bot 发一条消息（比如 `/start`），然后：

```bash
curl -sS --noproxy '*' "https://api.telegram.org/bot<token>/getUpdates" | grep -o '"chat":{"id":[-0-9]*'
```

令牌和 chat id **两个都配齐才生效**。只配一半不会报错——只会在启动日志里留一条 warning，因为"看起来配了、其实什么都没发"是最难查的那种。令牌格式也会在启动时检查（bot token 一律是 `<数字 id>:<密钥>`），格式不对直接拒绝启动并说明去哪拿，且报错信息里**不会回显令牌本身**。

触发方式有三种：

- **自动**：`pyexec_run` / `pyexec_run_isolated` / `pyexec_run_script` 的**墙钟耗时**超过 `PYEXEC_TELEGRAM_MIN_DURATION` 就发一条。用墙钟而不是 worker 自己统计的执行时间，因为值得通知的是"调用方等了多久"。
- **手动**：`pyexec_notify("...")` 发任意一条。适合自动阈值盖不住的情况——几十次短调用加起来的长活、或者开跑长任务前先报备。它等 Telegram 的响应，所以返回的 `ok` 是真的送达结果而不是"已排队"。
- **后台任务结束**：`pyexec_run_background` / `pyexec_run_script_background` 提交的任务跑完**一律通知**，无视 `PYEXEC_TELEGRAM_MIN_DURATION`，消息里带 job id 和产物的下载链接。阈值存在的理由是"阻塞调用的调用方手里已经有结果了，再来一条是重复"；而主动开后台这个动作本身就是在说"跑完告诉我"，调用方已经明确走开了，这条消息是唯一的报告。

消息长这样：

```
❌ pyexec_run_script FAILED in 2m12s
workstation · analysis/sweep.py
TimeoutError: Execution exceeded 120s and the worker was killed

starting pass 7

(output was truncated by the server's output cap)
```

后台任务的那条多一个 job id 和产物链接：

```
✅ pyexec_run_background finished in 8m41s
workstation · job-1a2b3c4d · nightly sweep
...
4000 rows written

🔗 http://192.168.1.10:8000/download/results/sweep.csv?exp=1759046400&sig=9f2c...
```

链接是从消息末尾往前**整条整条丢**的，不会被截成半截 URL；如果因为太长丢了，会写明还剩几条没列出来。链接排在最后是因为那是要点的东西，而截断正好从尾部开始——不这么处理的话，一条为了交付文件而发的消息反而会把文件弄丢。

几个刻意的选择：

- **通知失败绝不影响运行结果**。消息在结果生成之后、后台线程里发出，任何投递失败只记一条日志。聊天消息是顺手，返回值才是契约。
- **失败时给出 Telegram 自己的原话**（`chat not found`、`bot was blocked`），因为这两种毛病要在不同地方修。HTTP 200 但 body 里 `{"ok": false}` 也算失败——只看状态码会把这类错误当成功。
- **带的是输出的结尾而不是开头**：长任务有意义的部分在末尾——最后的进度、最终的数字、异常。而且 Telegram 本身有长度上限（4096），服务端先截到 3500 并注明截断。
- **令牌不进日志、也不进 `pyexec://config`**：配置里只暴露"启用了没有"、chat id 和阈值。
- **代理默认跟随环境变量**（`HTTPS_PROXY`）：需要代理才能连上 Telegram 的机器本来就已经配了代理。`PYEXEC_TELEGRAM_PROXY=` 显式设成空值则强制直连，用于"那个代理是给别的流量用的"这种情况。
- **纯文本、不带 `parse_mode`**：任意代码的输出里全是 Markdown/HTML 的元字符，为了不被 Telegram 拒收而层层转义，对读者一点好处也没有。
- 用标准库 `urllib` 发，不引额外的 HTTP 客户端，和 `dotenv.py` 的理由一样：这里只需要一个 form POST。

⚠️ 消息内容是**发到 Telegram 服务器**的，等同于发布而不是记日志。输出里如果有密钥、个人信息、客户数据，这条路径会把它带出去——不确定就别开，或者把阈值调高、只靠 `pyexec_notify` 手动发。

---

## 后台任务

阻塞式工具有个运行上限：`PYEXEC_MAX_TIMEOUT` 默认 300 秒。超时后先尝试中断代码；如果代码正阻塞在无法中断的调用中，服务端再等 5 秒便会终止 worker。这就是超时错误可能出现在 5 分 05 秒的原因。十分钟的 sweep 用阻塞方式跑不完，而且调用方得一直等待——客户端可能先超时，人也可能在等的时候就走开了。

后台任务是把"等待"从这次执行里拿掉：submit 立刻返回一个 job id，代码在**自己的一次性进程**里继续跑，跑完自动发通知，之后随时能按 id 取回完整结果。

```python
# 下达
pyexec_run_background(code, timeout=7200, artifacts=["results/sweep.csv"],
                      label="nightly sweep")
# -> ok=True, job_id='job-1a2b3c4d', state='running', notifies=True

# 查
pyexec_job_status("job-1a2b3c4d")    # state / 耗时 / 输出结尾 / 产物缺没缺
pyexec_job_status()                  # 不带参数：列出还记着的全部

# 取完整结果（通知里只有输出结尾）
pyexec_job_result("job-1a2b3c4d")
pyexec_job_result("job-1a2b3c4d", max_chars=4000)   # 话多的话只要结尾

# 不想要了
pyexec_job_cancel("job-1a2b3c4d")
```

几个刻意的选择：

- **每个任务一个独立进程，不碰持久会话**。持久会话是个硬串行点：一次调用整个往返期间持有非重入锁，worker 同时只应答一个请求。让后台任务共用它，要么卡住前台调用，要么被前台调用卡住。所以后台任务走的是 `pyexec_run_isolated` 那条一次性进程的路，代价是**拿不到会话里的变量**——要传数据进去就写文件。
- **submit 时就把进程拉起来**。多花一次解释器启动（约 100ms，相对一个跑几分钟的任务不算什么），换来两件事：返回后立刻 cancel 有进程可杀，不存在"取消撞上进程还没起来"的竞态；`PYEXEC_PYTHON` 配错了在 submit 当场报错，而不是只出现在一条可能没人看的通知里。
- **超时用单独的一套旋钮**（`PYEXEC_BACKGROUND_TIMEOUT` / `PYEXEC_BACKGROUND_MAX_TIMEOUT`，默认 1 小时 / 1 天）。后台任务的 worker 使用这套时限，不受 `PYEXEC_MAX_TIMEOUT` 的 300 秒上限限制；超过后台时限且 worker 无法自行中断时，服务端再等 5 秒便会终止它。
- **并发满了直接拒绝，不排队**。`PYEXEC_MAX_BACKGROUND_JOBS` 默认 4，每个任务都是整台子进程，这是限机器负载而不是限记账。第 5 个提交会返回 `ok=False` 并说明在跑几个，由调用方决定取消哪个——排队会让人以为任务迟早会跑，然后忘了它。
- **`ok` 是三态**。还在跑的任务 `pyexec_job_status` 里 `ok` 是 `null` 而不是 `false`：`false` 会被读成"失败了"然后说出去。
- **被拒绝的提交不会创建任务**。import 白名单没过、产物路径越界、脚本不存在、解释器起不来，都在 submit 阶段返回 `ok=False` + `reason`。让一个注定失败的任务跑起来再发一条失败通知，是双重浪费。

**结果在哪**：`PYEXEC_JOB_HISTORY` 默认保留最近 20 个已完成任务的完整 stdout/stderr，每个都受 `PYEXEC_MAX_OUTPUT_CHARS` 限制。超出的从最旧的已完成任务开始丢，在跑的任务永远不丢。

需要说清楚的几件事：

- **记录只在内存里**。服务端一重启，所有 job id 全部失效，查询会告诉你"记录只在内存里"。要留住结果就让它写文件，文件是落在工作区里的。
- **被取消的任务可能留下半个产物**。进程是直接杀的，没有机会 flush 缓冲或关文件。已经写了的内容留在工作区，server 不会去清理。任务自己起的孙进程也不会跟着一起死。
- **产物链接要有 `PYEXEC_BASE_URL` 才有用**。它和 `pyexec_get_download_url` 是同一套签名链接，只在客户端能真的访问到那个地址时才能点开；stdout 模式没有地址，链接就不出现。没写出来的产物不会生成链接，但会出现在 `missing_artifacts` 里——声明了却没产出是要说出来的。
- **`notified=true` 是"已交给投递线程"，不是"已送达"**。投递在后台线程里做，失败只记日志，所以同步的返回值不可能知道送没送到。
- **换配置会杀掉在跑的任务**。`PYEXEC_WORKSPACE` 一变，任务的 worker 工作目录就指向一个没人再看的地方了，所以配置替换时后台任务一律中止（且不发通知）。

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
- **限时签名下载链接**：下载地址只能访问签名覆盖的那一个文件，过期即失效，唯一密钥来自 `PYEXEC_AUTH_TOKEN`；路径同样是工作区囚笼

真要隔离，请用容器、虚拟机或受限用户账户——把整个 server 关在里面，而不是指望这一层。

---

## 架构

```
MCP client
    │  stdio / streamable-http
    ▼
server.py          MCPServer + 17 个工具，把 Settings、Session、JobManager 串起来
    │
    ├── auth.py       streamable-http 的 Bearer 中间件 + 启动联锁
    ├── downloads.py  /download 前置门：签名链接、路径囚笼、FileResponse
    ├── telegram.py   长时间运行结束后的 Telegram 通知（后台线程，失败只记日志）
    ├── jobs.py       后台任务登记表：独立进程、取消、有界历史
    ├── config.py     Settings：环境变量 → 配置，含令牌校验与 base URL 推导
    ├── dotenv.py     极简 .env 读取（不引 python-dotenv）
    ├── sandbox.py     AST import 白名单、路径囚笼（进 worker 之前）
    ├── packages.py    pip 安装 / 列举
    │
    ▼  一行一个 JSON，stdout 上带 nonce 的帧
session.py         每个会话一个 worker 子进程，串行化调用，超时硬杀；后台任务各用一个跑完即弃
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
- 后台任务的记录只在内存里，服务端重启即全部失效；被取消的任务可能在工作区留下半个产物文件。细节见上面的「后台任务」。
