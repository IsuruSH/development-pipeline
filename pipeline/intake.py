"""Stage 1 — Spec Intake.

Loads a specification from Markdown, YAML or JSON and validates it against
:class:`pipeline.state.FeatureSpec`. Fails fast with a precise, line-listed
error message when required fields are missing or malformed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .state import FeatureSpec


class SpecIntakeError(ValueError):
    """Raised when a spec file cannot be parsed or fails validation."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_spec(path: str | Path) -> FeatureSpec:
    """Parse + validate a spec file, returning a :class:`FeatureSpec`.

    Raises :class:`SpecIntakeError` on any failure.
    """

    p = Path(path)
    if not p.exists():
        raise SpecIntakeError(f"Spec file not found: {p}")

    raw = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()

    try:
        if suffix in {".yaml", ".yml"}:
            data = yaml.safe_load(raw)
        elif suffix == ".json":
            data = json.loads(raw)
        elif suffix in {".md", ".markdown"}:
            data = _parse_markdown(raw)
        else:
            raise SpecIntakeError(
                f"Unsupported spec format '{suffix}'. Use .yaml, .yml, .json or .md."
            )
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise SpecIntakeError(f"Failed to parse {p.name}: {exc}") from exc

    if not isinstance(data, dict):
        raise SpecIntakeError(
            f"Spec root must be a mapping/object, got {type(data).__name__}"
        )

    try:
        return FeatureSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecIntakeError(_format_validation_error(exc)) from exc


# ---------------------------------------------------------------------------
# Markdown parser
# ---------------------------------------------------------------------------

_MD_SECTION_MAP = {
    "name": "name",
    "feature name": "name",
    "objective": "objective",
    "feature objective": "objective",
    "user story": "user_story",
    "business rules": "business_rules",
    "acceptance criteria": "acceptance_criteria",
    "non-functional requirements": "non_functional_requirements",
    "nfr": "non_functional_requirements",
    "non functional requirements": "non_functional_requirements",
    "out of scope": "out_of_scope",
    "out-of-scope": "out_of_scope",
}

_LIST_FIELDS = {
    "business_rules",
    "non_functional_requirements",
    "out_of_scope",
}

_AC_RE = re.compile(r"^\s*(?:[-*]\s*)?(AC-\d+)\s*[:\-]\s*(.+)$")


def _parse_markdown(raw: str) -> dict[str, Any]:
    """Parse a markdown spec into the same dict shape YAML/JSON would produce.

    Recognises ``# Heading`` / ``## Heading`` sections. Bulleted lists become
    Python lists. Acceptance criteria of the form ``- AC-1: description`` are
    parsed into the structured ``{id, description}`` shape expected by
    :class:`AcceptanceCriterion`.
    """

    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if heading:
            title = heading.group(1).strip().lower().rstrip(":")
            current = _MD_SECTION_MAP.get(title)
            if current is not None:
                sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)

    result: dict[str, Any] = {}
    for field, lines in sections.items():
        text = "\n".join(lines).strip()
        if not text:
            continue

        if field == "acceptance_criteria":
            criteria = []
            for ln in lines:
                m = _AC_RE.match(ln)
                if m:
                    criteria.append({"id": m.group(1), "description": m.group(2).strip()})
            result[field] = criteria
        elif field in _LIST_FIELDS:
            items = []
            for ln in lines:
                m = re.match(r"^\s*[-*]\s+(.+?)\s*$", ln)
                if m:
                    items.append(m.group(1))
            result[field] = items or [text]
        else:
            result[field] = text

    return result


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["Spec failed validation:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
