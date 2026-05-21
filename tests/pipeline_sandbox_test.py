"""Unit tests for the sandbox guard.

These are *pipeline* tests (not generated tests). They are prefixed
``pipeline_`` so the Test Generator stage cannot overwrite them when it
populates ``tests/test_*.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.sandbox import SandboxViolation, guard, write


def test_allows_simple_relative_path(tmp_path: Path) -> None:
    resolved = guard("src/auth.py", allowed_root=tmp_path)
    assert resolved == (tmp_path / "src/auth.py").resolve()


def test_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        guard("/etc/passwd", allowed_root=tmp_path)


def test_rejects_windows_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        guard("C:/Windows/System32/cmd.exe", allowed_root=tmp_path)


def test_rejects_traversal_segment(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        guard("src/../../etc/passwd", allowed_root=tmp_path)


def test_rejects_home_relative(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        guard("~/escape.py", allowed_root=tmp_path)


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    written = write("src/deep/nested/file.py", "x = 1\n", allowed_root=tmp_path)
    assert written.read_text(encoding="utf-8") == "x = 1\n"
    assert written.parent.is_dir()
