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

#: Flag -> environment variable. The flag wins when both are present.
ENV_KEYS = {
    "workspace": "PYEXEC_WORKSPACE",
    "python": "PYEXEC_PYTHON",
    "timeout": "PYEXEC_TIMEOUT",
    "max_timeout": "PYEXEC_MAX_TIMEOUT",
    "max_output_chars": "PYEXEC_MAX_OUTPUT_CHARS",
    "allowed_modules": "PYEXEC_ALLOWED_MODULES",
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
        default=os.environ.get("PYEXEC_HOST", "0.0.0.0"),
        help="Bind address for the streamable-http transport.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PYEXEC_PORT", "8000")),
        help="Port for the streamable-http transport.",
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
        "--allowed-modules",
        help=(
            "Comma-separated allowlist of importable top-level modules. "
            "Unset means no restriction."
        ),
    )
    parser.add_argument(
        "--no-inherit-env",
        action="store_true",
        help="Run workers with a scrubbed environment instead of the server's.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    for flag, env_key in ENV_KEYS.items():
        value = getattr(args, flag)
        if value is not None:
            os.environ[env_key] = str(value)

    if args.no_inherit_env:
        os.environ["PYEXEC_INHERIT_ENV"] = "0"

    if args.transport == "stdio":
        server.main(transport="stdio")
    else:
        server.main(transport="streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
