"""Versioned prompt template registry.

Each prompt is a plain ``.md`` file in this package. The registry loads it
once and returns ``(template, version_id)`` where ``version_id`` is the first
8 hex chars of the sha256 of the file content. The version is stamped into
every audit event so a run is exactly reproducible.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    template: str
    path: Path

    def render(self, **kwargs: object) -> str:
        """Render the template using ``str.format(**kwargs)``.

        We use the standard ``format`` mini-language. JSON braces in prompt
        examples must be escaped as ``{{`` / ``}}`` in the source files.
        """
        return self.template.format(**kwargs)


@cache
def get(name: str) -> PromptTemplate:
    """Load prompt ``name`` (without the ``.md`` extension)."""

    # We always pick the latest ``v<N>`` file for ``name``.
    candidates = sorted(_PROMPTS_DIR.glob(f"{name}.v*.md"))
    if not candidates:
        raise FileNotFoundError(f"No prompt template found for '{name}' in {_PROMPTS_DIR}")
    path = candidates[-1]
    text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    version = f"{path.stem.rsplit('.', 1)[-1]}+{digest}"  # e.g. "v1+a1b2c3d4"
    return PromptTemplate(name=name, version=version, template=text, path=path)


def list_all() -> list[PromptTemplate]:
    """Return every prompt template currently registered. Useful for audit."""
    names = {p.stem.split(".", 1)[0] for p in _PROMPTS_DIR.glob("*.v*.md")}
    return [get(n) for n in sorted(names)]
