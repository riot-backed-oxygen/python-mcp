"""Dependency management for the interpreter the workers run on.

Installs target the same interpreter the workers use, so a package installed
here is importable by the very next ``pyexec_run`` call.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from .config import Settings
from .sandbox import GuardrailError, check_package_specs

#: Installing is slow; this is a ceiling, not a target.
INSTALL_TIMEOUT = 300.0


@dataclass
class InstallResult:
    ok: bool
    command: str
    stdout: str
    stderr: str
    returncode: int


def _install_command(settings: Settings, upgrade: bool) -> list[str]:
    """Prefer uv when it is on PATH.

    uv is both faster and available in environments that have no pip at all --
    ``uv venv`` does not install pip, which is exactly the setup this project
    tells you to use, so falling back to ``python -m pip`` alone would fail on
    the recommended install.
    """
    uv = shutil.which("uv")
    if uv:
        command = [uv, "pip", "install", "--python", settings.python_executable]
    else:
        command = [
            settings.python_executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
        ]

    if upgrade:
        command.append("--upgrade")
    return command


def install(
    settings: Settings, packages: list[str], upgrade: bool = False
) -> InstallResult:
    """Install *packages* into the interpreter the workers use.

    Raises:
        GuardrailError: if a spec looks like a pip flag rather than a package.
    """
    check_package_specs(packages)

    command = _install_command(settings, upgrade)
    command.extend(packages)

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=INSTALL_TIMEOUT,
            env=settings.child_env(),
        )
    except subprocess.TimeoutExpired:
        return InstallResult(
            ok=False,
            command=" ".join(command),
            stdout="",
            stderr=f"Install did not finish within {INSTALL_TIMEOUT:g}s.",
            returncode=-1,
        )

    return InstallResult(
        ok=completed.returncode == 0,
        command=" ".join(command),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        returncode=completed.returncode,
    )


def list_installed(settings: Settings, match: str | None = None) -> list[str]:
    """Return ``name==version`` for every distribution the workers can import.

    Args:
        match: optional case-insensitive substring filter.
    """
    script = (
        "import importlib.metadata as m, json, sys;"
        "print(json.dumps(sorted("
        "f'{d.metadata[\"Name\"]}=={d.version}' "
        "for d in m.distributions() if d.metadata['Name'])))"
    )
    try:
        completed = subprocess.run(
            [settings.python_executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=settings.child_env(),
        )
    except subprocess.TimeoutExpired:
        return []

    if completed.returncode != 0:
        return []

    try:
        packages: list[str] = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return []

    if match:
        needle = match.lower()
        packages = [p for p in packages if needle in p.lower()]
    return packages


__all__ = [
    "GuardrailError",
    "InstallResult",
    "install",
    "list_installed",
]
