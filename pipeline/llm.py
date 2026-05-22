"""Provider-agnostic LLM layer.

The pipeline supports three back-ends, all configured via environment
variables so the same code runs against any of them without edits:

| ``LLM_PROVIDER`` | Backend                                      |
| ---------------- | -------------------------------------------- |
| ``ollama``       | Local Ollama via its OpenAI-compatible API   |
| ``openai``       | Real OpenAI or any OpenAI-compatible endpoint|
| ``anthropic``    | Anthropic Claude SDK                         |

Relevant environment variables:

- ``LLM_PROVIDER``  — provider id (default: ``ollama``).
- ``LLM_MODEL``     — model id (e.g. ``qwen2.5-coder:1.5b``,
  ``gpt-4o-mini``, ``claude-sonnet-4-5``). The CLI ``--model`` flag
  overrides this.
- ``LLM_BASE_URL``  — base URL for OpenAI-compatible providers.
  Defaults: ``http://localhost:11434/v1`` for ollama,
  ``https://api.openai.com/v1`` for openai. Ignored for anthropic.
- ``LLM_API_KEY``   — API key. For ollama any string works (default
  ``ollama``). For openai/anthropic this is required.

For backwards compatibility, ``ANTHROPIC_API_KEY`` and ``OPENAI_API_KEY``
are also recognised when the matching provider is selected.

Public surface used by the rest of the pipeline:

- :func:`call_json` — call the model and parse the response as JSON.
- :func:`call_text` — call the model and return raw text.
- :func:`default_model` — pick a sensible model id for the configured
  provider when the caller didn't specify one (``model="auto"``).
- :class:`LLMUsage`  — token-count + cost + latency record.
- :class:`LLMError`  — raised on any provider failure (after retries).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Pricing — USD per million tokens (input, output).
# ---------------------------------------------------------------------------

_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "claude-opus-4": (15.00, 75.00),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "o4-mini": (1.10, 4.40),
    # Local models are free
    "tinyllama": (0.0, 0.0),
    "llama3.2": (0.0, 0.0),
    "llama3.2:1b": (0.0, 0.0),
    "llama3.2:3b": (0.0, 0.0),
    "qwen2.5-coder": (0.0, 0.0),
    "qwen2.5-coder:1.5b": (0.0, 0.0),
    "qwen2.5-coder:7b": (0.0, 0.0),
    "phi3:mini": (0.0, 0.0),
}
_DEFAULT_PRICE = (0.0, 0.0)

# ---------------------------------------------------------------------------
# Per-provider defaults
# ---------------------------------------------------------------------------

_PROVIDER_DEFAULT_MODEL = {
    # qwen2.5-coder:1.5b is a small (~986MB) code-focused model that
    # produces valid JSON reliably enough for the planner/implementer
    # stages. tinyllama is supported but too small for strict JSON output.
    "ollama": "qwen2.5-coder:1.5b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
}

_PROVIDER_DEFAULT_BASE_URL = {
    "ollama": "http://localhost:11434/v1",
    "openai": "https://api.openai.com/v1",
}

_SUPPORTED_PROVIDERS = set(_PROVIDER_DEFAULT_MODEL.keys())


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class LLMUsage:
    model: str
    provider: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    cost_usd: float
    raw_text: str


class LLMError(RuntimeError):
    """Raised when the LLM call fails after retries."""


@dataclass(frozen=True)
class _Config:
    provider: str
    model: str
    base_url: str
    api_key: str


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def default_model() -> str:
    """Return the default model id for the currently configured provider."""
    provider = _resolve_provider()
    return os.environ.get("LLM_MODEL") or _PROVIDER_DEFAULT_MODEL.get(
        provider, "qwen2.5-coder:1.5b"
    )


def _resolve_provider() -> str:
    provider = (os.environ.get("LLM_PROVIDER") or "ollama").strip().lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise LLMError(
            f"Unsupported LLM_PROVIDER={provider!r}. "
            f"Choose one of: {sorted(_SUPPORTED_PROVIDERS)}"
        )
    return provider


def _resolve_config(model: str) -> _Config:
    provider = _resolve_provider()

    # If caller passed model="auto" or "", fall back to env or provider default.
    if model in ("", "auto"):
        model = default_model()

    base_url = os.environ.get("LLM_BASE_URL") or _PROVIDER_DEFAULT_BASE_URL.get(provider, "")
    # Strip a trailing slash so we can safely append "/chat/completions".
    base_url = base_url.rstrip("/")

    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get(f"{provider.upper()}_API_KEY")
        or ""
    )
    if provider == "ollama" and not api_key:
        # Ollama doesn't check the key but the OpenAI client requires one.
        api_key = "ollama"

    if provider in {"openai", "anthropic"} and not api_key:
        raise LLMError(
            f"{provider.upper()}_API_KEY (or LLM_API_KEY) is not set. "
            "Add it to your environment or .env file."
        )
    if provider in {"ollama", "openai"} and not base_url:
        raise LLMError(f"LLM_BASE_URL is not set for provider={provider!r}.")

    return _Config(provider=provider, model=model, base_url=base_url, api_key=api_key)


# ---------------------------------------------------------------------------
# Public call helpers
# ---------------------------------------------------------------------------


def call_json(
    *,
    prompt: str,
    model: str,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    system: str | None = None,
) -> tuple[dict[str, Any], LLMUsage]:
    """Call the configured provider and parse the response as JSON.

    Hardening layers, applied in order:

    1. **JSON mode at the decoder.** Pass ``response_format={"type":
       "json_object"}`` so Ollama / OpenAI constrain the model to emit a
       valid JSON object. This stops the most common small-model failure
       (Python-style triple quotes inside JSON) at generation time.
    2. **Heuristic repair.** If the response still isn't parseable
       (e.g. the model ignored the format flag), :func:`_extract_json`
       attempts a small set of repairs — most importantly turning
       ``\"\"\"...\"\"\"`` blocks into properly escaped JSON strings.
    3. **One retry with a sharper prompt.** Last resort, append an
       explicit "you must return only JSON" note and try once more.
    """

    cfg = _resolve_config(model)
    base_attempt = prompt
    last_err: Exception | None = None

    for attempt_idx in range(2):  # one retry slot
        attempt_prompt = base_attempt if attempt_idx == 0 else (
            base_attempt
            + "\n\nIMPORTANT: Your previous response was not valid JSON. "
            "Reply with ONLY a single JSON object — no markdown fences, no prose, "
            'and NEVER use Python triple-quoted strings ("""..."""). Inside JSON, '
            "multi-line content must use \\n escapes inside normal double-quoted strings."
        )

        usage = _dispatch(cfg, attempt_prompt, max_tokens, temperature, system, json_mode=True)
        try:
            parsed = _extract_json(usage.raw_text)
            return parsed, usage
        except ValueError as exc:
            last_err = exc
            continue

    raise LLMError(
        f"Model did not return valid JSON after retry: {last_err}\n"
        f"Last raw output (truncated):\n{usage.raw_text[:500]}"
    )


def call_text(
    *,
    prompt: str,
    model: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    system: str | None = None,
) -> LLMUsage:
    """Call the configured provider and return raw text (no JSON parsing)."""
    cfg = _resolve_config(model)
    return _dispatch(cfg, prompt, max_tokens, temperature, system, json_mode=False)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _dispatch(
    cfg: _Config,
    prompt: str,
    max_tokens: int,
    temperature: float,
    system: str | None,
    *,
    json_mode: bool = False,
) -> LLMUsage:
    if cfg.provider == "anthropic":
        # Claude has no `response_format` flag, but it follows JSON
        # instructions reliably enough that we don't need post-hoc constraints.
        return _call_anthropic(cfg, prompt, max_tokens, temperature, system)
    # ollama + openai both speak the OpenAI Chat Completions API.
    return _call_openai_compatible(
        cfg, prompt, max_tokens, temperature, system, json_mode=json_mode
    )


# ---------------------------------------------------------------------------
# OpenAI-compatible backend (covers ollama + openai + most others)
# ---------------------------------------------------------------------------


def _call_openai_compatible(
    cfg: _Config,
    prompt: str,
    max_tokens: int,
    temperature: float,
    system: str | None,
    *,
    json_mode: bool = False,
) -> LLMUsage:
    """POST to ``{base_url}/chat/completions``.

    The same wire format works for real OpenAI, Ollama, vLLM, LM Studio,
    Groq, Together, Anyscale and other OpenAI-compatible endpoints.

    When ``json_mode=True`` we ask the backend to constrain decoding to a
    valid JSON object via ``response_format={"type": "json_object"}``.
    Ollama (>=0.1.30) and OpenAI both honour this; other providers ignore
    unknown fields, so it's safe to send unconditionally.
    """

    url = f"{cfg.base_url}/chat/completions"
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.api_key}",
    }

    started = time.perf_counter()
    try:
        # Local models can take a while on first call (model load); be generous.
        timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
        resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LLMError(f"{cfg.provider} HTTP error: {exc}") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    if resp.status_code >= 400:
        # Try to surface a useful error body without dumping the whole prompt back.
        body_preview = resp.text[:400]
        raise LLMError(
            f"{cfg.provider} returned HTTP {resp.status_code}: {body_preview}"
        )

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise LLMError(f"{cfg.provider} returned non-JSON response: {exc}") from exc

    try:
        raw_text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"{cfg.provider} response missing choices/message: {data!r}") from exc

    usage_info = data.get("usage") or {}
    tokens_in = int(usage_info.get("prompt_tokens", 0) or 0)
    tokens_out = int(usage_info.get("completion_tokens", 0) or 0)

    return LLMUsage(
        model=cfg.model,
        provider=cfg.provider,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        cost_usd=_estimate_cost(cfg.model, tokens_in, tokens_out),
        raw_text=raw_text.strip(),
    )


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------


def _call_anthropic(
    cfg: _Config,
    prompt: str,
    max_tokens: int,
    temperature: float,
    system: str | None,
) -> LLMUsage:
    """Call Claude through the official Anthropic SDK."""
    try:
        from anthropic import Anthropic, APIError, APIStatusError
    except ImportError as exc:  # pragma: no cover
        raise LLMError(
            "anthropic SDK is not installed. `pip install anthropic` or use a different provider."
        ) from exc

    client = Anthropic(api_key=cfg.api_key)
    started = time.perf_counter()
    try:
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        msg = client.messages.create(**kwargs)
    except APIStatusError as exc:
        raise LLMError(f"Anthropic API error ({exc.status_code}): {exc.message}") from exc
    except APIError as exc:
        raise LLMError(f"Anthropic API error: {exc}") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    text_parts: list[str] = []
    for block in msg.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            text_parts.append(text)
    raw_text = "".join(text_parts).strip()

    tokens_in = getattr(msg.usage, "input_tokens", 0) or 0
    tokens_out = getattr(msg.usage, "output_tokens", 0) or 0

    return LLMUsage(
        model=cfg.model,
        provider=cfg.provider,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        cost_usd=_estimate_cost(cfg.model, tokens_in, tokens_out),
        raw_text=raw_text,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    in_price, out_price = _PRICE_TABLE.get(model, _DEFAULT_PRICE)
    return (tokens_in / 1_000_000) * in_price + (tokens_out / 1_000_000) * out_price


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_TRIPLE_QUOTED_RE = re.compile(r'"""(.*?)"""', re.DOTALL)


def _extract_json(raw: str) -> dict[str, Any]:
    """Extract a single JSON object from a model response.

    Tolerates the model wrapping its JSON in a ```json fenced block or
    surrounding it with explanatory prose. As a last resort, runs a small
    repair pass for the most common small-model failure: Python-style
    triple-quoted strings used as JSON string values.
    """

    text = raw.strip()
    fence_match = _JSON_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("no JSON object found in response")
        text = text[start : end + 1]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as first_exc:
        # Repair pass — only worth attempting if the failure looks like the
        # triple-quote bug (otherwise we'd just be re-raising slightly later).
        if '"""' not in text:
            raise ValueError(f"invalid JSON: {first_exc}") from first_exc
        repaired = _repair_triple_quoted_strings(text)
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError as repair_exc:
            # Surface the original failure — the repair pass is a heuristic,
            # not a guarantee.
            raise ValueError(f"invalid JSON: {first_exc}") from repair_exc

    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _repair_triple_quoted_strings(text: str) -> str:
    """Turn Python-style ``\"\"\"...\"\"\"`` blocks into proper JSON strings.

    Small open-source models (qwen2.5-coder:1.5b, llama3.2:1b, ...)
    occasionally reach for triple-quoted Python string literals when
    asked to embed multi-line code in a JSON field. ``json.dumps``
    handles every escape (``\\\\``, ``\"``, ``\\n``, ``\\t``, control
    characters) for us, so the repair is short:

    >>> repaired = _repair_triple_quoted_strings('{"x": \"\"\"a\\nb\"\"\"}')
    >>> json.loads(repaired)
    {'x': 'a\\nb'}
    """

    def _to_json_string(match: re.Match[str]) -> str:
        return json.dumps(match.group(1))

    return _TRIPLE_QUOTED_RE.sub(_to_json_string, text)
