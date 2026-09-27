"""Guardrails applied before code or paths reach a worker.

Read this as an accident-prevention layer, not a security boundary. The whole
point of this server is to run arbitrary Python, and arbitrary Python can walk
around any check that lives inside the same interpreter. Real isolation has to
come from the OS -- a container, a VM, a restricted user account -- not from
this module.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


class GuardrailError(Exception):
    """A request was rejected before any code ran."""


#: A distribution specifier: name, optional extras, optional version pin.
_PACKAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"      # distribution name
    r"(\[[A-Za-z0-9._,-]+\])?"          # optional extras
    r"\s*([<>=!~]=?[^,;]*)?$"           # optional version specifier
)


def _import_roots(tree: ast.AST) -> set[str]:
    """Top-level module names imported anywhere in *tree*."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import; no absolute root to check
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            # ``importlib.import_module("x")`` / ``__import__("x")`` reach the
            # same place as an import statement, so treat literal arguments the
            # same way. Dynamic strings are not catchable here.
            name = _called_name(node.func)
            if name in {"__import__", "import_module"} and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    roots.add(arg.value.split(".")[0])
    return roots


def _called_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def check_code(code: str, allowed_modules: frozenset[str] | None) -> None:
    """Reject *code* that fails to parse, or imports outside the allowlist.

    Raises:
        GuardrailError: with a message naming the offending modules so the
            caller can fix the snippet or widen ``PYEXEC_ALLOWED_MODULES``.
    """
    try:
        tree = ast.parse(code, "<pyexec>", "exec")
    except SyntaxError as exc:
        raise GuardrailError(
            f"SyntaxError on line {exc.lineno}: {exc.msg}"
        ) from exc

    if allowed_modules is None:
        return

    blocked = sorted(_import_roots(tree) - allowed_modules)
    if blocked:
        raise GuardrailError(
            "Import blocked by PYEXEC_ALLOWED_MODULES: "
            + ", ".join(blocked)
            + ". Add them to that variable to allow them, or unset it to lift "
            "the restriction entirely."
        )


def check_package_specs(specs: list[str]) -> None:
    """Reject package arguments that could be read as pip options.

    Raises:
        GuardrailError: if any spec looks like a flag or a path.
    """
    for spec in specs:
        if not _PACKAGE_RE.match(spec.strip()):
            raise GuardrailError(
                f"Not a valid package specifier: {spec!r}. Expected a name like "
                "'pandas' or 'pandas==2.2.0'."
            )


def resolve_within(path: str, root: Path, allow_outside: bool = False) -> Path:
    """Resolve *path* against *root*, refusing to escape it.

    Raises:
        GuardrailError: if the resolved path lands outside *root* and
            *allow_outside* is false.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()

    if allow_outside:
        return resolved

    if resolved != root and root not in resolved.parents:
        raise GuardrailError(
            f"Path escapes the workspace: {path!r} resolves to {resolved}, "
            f"which is outside {root}. Use a path inside the workspace, or set "
            "PYEXEC_WORKSPACE to a directory that contains it."
        )
    return resolved
