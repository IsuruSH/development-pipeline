"""Filesystem sandbox for AI-generated writes.

The Implementer (and Test Generator) ask Claude for paths like
``src/auth.py`` or ``tests/test_auth.py``. Before we ever touch the disk,
those paths flow through :func:`guard`, which:

1. Rejects absolute paths.
2. Rejects paths containing ``..`` traversal segments.
3. Resolves the final path and verifies it is *strictly inside* the
   ``allowed_root`` directory using :meth:`pathlib.Path.is_relative_to`.

Any violation raises :class:`SandboxViolation`, which the orchestrator
treats as a halt-worthy error.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

# Matches Windows drive letters at the start of a path, e.g. ``C:`` or ``c:/``.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class SandboxViolation(RuntimeError):
    """Raised when a generated file would be written outside the sandbox."""


def guard(path: str | Path, allowed_root: Path) -> Path:
    """Resolve ``path`` and verify it is inside ``allowed_root``.

    Returns the fully-resolved absolute path, ready for writing. Does NOT
    create any directories or touch the disk.
    """

    raw = str(path).replace("\\", "/")

    if _WINDOWS_DRIVE_RE.match(raw):
        raise SandboxViolation(f"Absolute (drive-letter) paths are forbidden: {raw!r}")

    pp = PurePosixPath(raw)

    if pp.is_absolute():
        raise SandboxViolation(f"Absolute paths are forbidden: {raw!r}")

    if any(part == ".." for part in pp.parts):
        raise SandboxViolation(f"Path traversal ('..') is forbidden: {raw!r}")

    if any(part.startswith("~") for part in pp.parts):
        raise SandboxViolation(f"Home-relative paths are forbidden: {raw!r}")

    allowed_root_resolved = allowed_root.resolve()
    candidate = (allowed_root_resolved / Path(*pp.parts)).resolve()

    if not _is_relative_to(candidate, allowed_root_resolved):
        raise SandboxViolation(
            f"Refusing write outside sandbox '{allowed_root_resolved}': {raw!r}"
        )

    return candidate


def write(path: str | Path, content: str, allowed_root: Path) -> Path:
    """Convenience wrapper that guards, mkdirs, and writes UTF-8 text."""
    resolved = guard(path, allowed_root)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return resolved


def _is_relative_to(child: Path, parent: Path) -> bool:
    """Backport-safe ``Path.is_relative_to`` (added in 3.9)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
