"""Command-line entry point.

Flags are translated into the same environment variables :class:`Settings`
reads, so the CLI and an MCP client's ``env`` block are two spellings of one
configuration rather than two code paths.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import server
from .config import DEFAULT_HOST, DEFAULT_PORT

#: Flag -> environment variable. The flag wins when both are present.
#:
#: ``host`` and ``port`` are here because the download links this server hands
#: out are built from the address clients reach it at, which is derived from
#: them. Leaving them out would let ``--port`` disagree with the URL the tools
#: produce.
ENV_KEYS = {
    "host": "PYEXEC_HOST",
    "port": "PYEXEC_PORT",
    "base_url": "PYEXEC_BASE_URL",
    "workspace": "PYEXEC_WORKSPACE",
    "python": "PYEXEC_PYTHON",
    "timeout": "PYEXEC_TIMEOUT",
    "max_timeout": "PYEXEC_MAX_TIMEOUT",
    "max_output_chars": "PYEXEC_MAX_OUTPUT_CHARS",
    "background_timeout": "PYEXEC_BACKGROUND_TIMEOUT",
    "background_max_timeout": "PYEXEC_BACKGROUND_MAX_TIMEOUT",
    "max_background_jobs": "PYEXEC_MAX_BACKGROUND_JOBS",
    "job_history": "PYEXEC_JOB_HISTORY",
    "download_ttl": "PYEXEC_DOWNLOAD_TTL",
    "allowed_modules": "PYEXEC_ALLOWED_MODULES",
    "auth_token": "PYEXEC_AUTH_TOKEN",
    "auth_token_file": "PYEXEC_AUTH_TOKEN_FILE",
    "telegram_bot_token": "PYEXEC_TELEGRAM_BOT_TOKEN",
    "telegram_bot_token_file": "PYEXEC_TELEGRAM_BOT_TOKEN_FILE",
    "telegram_chat_id": "PYEXEC_TELEGRAM_CHAT_ID",
    "telegram_min_duration": "PYEXEC_TELEGRAM_MIN_DURATION",
    "telegram_proxy": "PYEXEC_TELEGRAM_PROXY",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyexec-mcp",
        description=(
            "An MCP server that executes Python code. Defaults to the stdio "
            "transport, which is what MCP clients expect."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.environ.get("PYEXEC_TRANSPORT", "stdio"),
        help="stdio for a local client, streamable-http to listen on a port.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("PYEXEC_HOST", DEFAULT_HOST),
        help="Bind address for the streamable-http transport.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PYEXEC_PORT", str(DEFAULT_PORT))),
        help="Port for the streamable-http transport.",
    )
    parser.add_argument(
        "--base-url",
        help=(
            "Address clients use to reach this server, used to build the "
            "download links pyexec_get_download_url returns. Defaults to the "
            "bind address above; set it when that is not what clients dial "
            "(NAT, port forward, reverse proxy)."
        ),
    )
    parser.add_argument(
        "--download-ttl",
        type=float,
        help=(
            "Seconds a signed download link stays valid. Also the ceiling for "
            "the lifetime a tool call asks for."
        ),
    )
    parser.add_argument(
        "--workspace",
        help="Root the file tools are confined to, and the worker's cwd.",
    )
    parser.add_argument(
        "--python",
        help="Interpreter to execute code with. Defaults to the one running the server.",
    )
    parser.add_argument(
        "--timeout", type=float, help="Default per-call timeout in seconds."
    )
    parser.add_argument(
        "--max-timeout", type=float, help="Ceiling for a caller-supplied timeout."
    )
    parser.add_argument(
        "--max-output-chars", type=int, help="Per-stream output retained before truncation."
    )
    parser.add_argument(
        "--background-timeout",
        type=float,
        help=(
            "Default timeout for a background job, in seconds. Kept separate "
            "from --timeout because escaping that ceiling is the whole point of "
            "running something in the background."
        ),
    )
    parser.add_argument(
        "--background-max-timeout",
        type=float,
        help=(
            "Ceiling for a timeout a caller passes to a background job. Higher "
            "than --max-timeout for the same reason, and worth keeping finite: a "
            "forgotten job otherwise holds its slot indefinitely."
        ),
    )
    parser.add_argument(
        "--max-background-jobs",
        type=int,
        help=(
            "How many background jobs may run at once. Each is a whole "
            "subprocess, so this bounds the machine's load. Further submissions "
            "are refused rather than queued."
        ),
    )
    parser.add_argument(
        "--job-history",
        type=int,
        help=(
            "How many finished background jobs stay readable. Records live in "
            "memory only, so this also bounds what a restart would cost."
        ),
    )
    parser.add_argument(
        "--allowed-modules",
        help=(
            "Comma-separated allowlist of importable top-level modules. "
            "Unset means no restriction."
        ),
    )
    parser.add_argument(
        "--auth-token",
        help=(
            "Shared secret HTTP clients must send as 'Authorization: Bearer'. "
            "Required when --host is not loopback. Prefer the "
            "PYEXEC_AUTH_TOKEN environment variable, or --auth-token-file, so "
            "the secret stays out of the process listing."
        ),
    )
    parser.add_argument(
        "--auth-token-file",
        help=(
            "Read the bearer token from this file instead of the command line. "
            "Trailing whitespace is stripped."
        ),
    )
    parser.add_argument(
        "--telegram-bot-token",
        help=(
            "Telegram bot token from @BotFather. With --telegram-chat-id, long "
            "runs announce themselves in that chat. Prefer the environment "
            "variable or --telegram-bot-token-file, so the secret stays out of "
            "the process listing."
        ),
    )
    parser.add_argument(
        "--telegram-bot-token-file",
        help="Read the Telegram bot token from this file instead.",
    )
    parser.add_argument(
        "--telegram-chat-id",
        help=(
            "Chat the notifications go to. A bot cannot find this on its own: "
            "message it once and read the id from getUpdates."
        ),
    )
    parser.add_argument(
        "--telegram-min-duration",
        type=float,
        help=(
            "Seconds a run must take before it is announced. 0 announces every "
            "run."
        ),
    )
    parser.add_argument(
        "--telegram-proxy",
        help=(
            "Proxy for reaching api.telegram.org, e.g. http://127.0.0.1:7890. "
            "Defaults to the environment's HTTPS_PROXY; pass an empty value to "
            "connect directly."
        ),
    )
    parser.add_argument(
        "--no-inherit-env",
        action="store_true",
        help="Run workers with a scrubbed environment instead of the server's.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Before build_parser(), because several flag defaults come from the
    # environment -- PYEXEC_TRANSPORT, PYEXEC_HOST and PYEXEC_PORT among them.
    # server.main() loads it too; a second pass is a no-op.
    from . import dotenv

    dotenv.load(os.environ.get("PYEXEC_ENV_FILE", dotenv.DEFAULT_ENV_FILENAME))

    args = build_parser().parse_args(argv)

    for flag, env_key in ENV_KEYS.items():
        value = getattr(args, flag)
        if value is not None:
            os.environ[env_key] = str(value)

    if args.no_inherit_env:
        os.environ["PYEXEC_INHERIT_ENV"] = "0"

    server.main(
        transport=args.transport,
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
