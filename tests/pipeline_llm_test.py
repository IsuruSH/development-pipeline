"""Unit tests for the LLM layer's JSON-handling helpers.

These cover the post-hoc repair logic that turns common small-model
mistakes (Python triple-quoted strings, fenced JSON blocks, prose
prefixes) into valid JSON we can parse downstream. The actual network
call (``call_json``) is exercised end-to-end by the pipeline; these
tests pin the pure-function behaviour.
"""

from __future__ import annotations

import pytest

from pipeline.llm import _extract_json, _repair_triple_quoted_strings


def test_extract_plain_json_object() -> None:
    parsed = _extract_json('{"x": 1, "y": "hello"}')
    assert parsed == {"x": 1, "y": "hello"}


def test_extract_strips_fenced_block() -> None:
    raw = '```json\n{"x": 1}\n```'
    assert _extract_json(raw) == {"x": 1}


def test_extract_strips_prose_around_object() -> None:
    raw = 'Sure, here is the JSON you asked for:\n{"x": 1}\nLet me know if you need more.'
    assert _extract_json(raw) == {"x": 1}


def test_repair_handles_triple_quoted_content() -> None:
    # The failure mode we actually saw against qwen2.5-coder:1.5b — model
    # used Python `"""..."""` for a multi-line string in the JSON value.
    broken = (
        '{"files": [{"path": "tests/test_x.py", '
        '"content": """import pytest\ndef test_ok():\n    assert True\n"""}]}'
    )
    parsed = _extract_json(broken)
    assert parsed["files"][0]["path"] == "tests/test_x.py"
    assert "import pytest" in parsed["files"][0]["content"]
    assert "\n" in parsed["files"][0]["content"]


def test_repair_round_trips_quotes_and_backslashes() -> None:
    # Content that mixes quotes and backslashes must be re-escaped correctly.
    broken = '{"content": """a "b" c \\d"""}'
    parsed = _extract_json(broken)
    assert parsed["content"] == 'a "b" c \\d'


def test_repair_helper_is_idempotent_on_clean_input() -> None:
    clean = '{"x": "y"}'
    assert _repair_triple_quoted_strings(clean) == clean


def test_extract_rejects_array_root() -> None:
    # A bare array isn't a JSON object — both branches of the guard
    # (no leading `{` and "not a dict") cover this; we only need one to fire.
    with pytest.raises(ValueError):
        _extract_json("[1, 2, 3]")


def test_extract_reports_invalid_json_clearly() -> None:
    with pytest.raises(ValueError, match="invalid JSON"):
        _extract_json("{not even close")
