"""Startup hook that guarantees the configured Ollama model is available.

Replaces the previous ``ollama-pull`` docker-compose service, which depended
on ``condition: service_completed_successfully`` — a feature that hangs in
some Docker Compose versions. Doing the check from inside the pipeline
container is more robust:

- Pure Python (httpx is already a dep).
- Talks to the Ollama HTTP API directly, no Ollama CLI required.
- Streams pull progress to the console so the user knows what's happening.
- Cheap no-op (~50 ms) when the model is already cached.
- Only runs when ``LLM_PROVIDER=ollama``; OpenAI/Anthropic providers skip.
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import urlparse

import httpx
from rich.console import Console


class ModelPullError(RuntimeError):
    """Raised when the model can't be located or pulled."""


def _api_base_from_chat_url(base_url: str) -> str:
    """Turn the OpenAI-compatible chat URL into the native Ollama API base.

    ``http://ollama:11434/v1`` -> ``http://ollama:11434``
    ``http://localhost:11434`` -> ``http://localhost:11434``
    """
    parsed = urlparse(base_url.rstrip("/"))
    if not parsed.scheme or not parsed.netloc:
        raise ModelPullError(f"LLM_BASE_URL is not a valid URL: {base_url!r}")
    # Drop the path (typically "/v1"); ollama's native API is at the root.
    return f"{parsed.scheme}://{parsed.netloc}"


def _model_is_cached(api_base: str, model: str, client: httpx.Client) -> bool:
    """Return True if Ollama reports ``model`` in its local catalogue."""
    try:
        resp = client.get(f"{api_base}/api/tags", timeout=10.0)
    except httpx.HTTPError as exc:
        raise ModelPullError(
            f"could not reach Ollama at {api_base}: {exc}. "
            "Is the ollama container up? Try `docker start spec-ollama`."
        ) from exc

    if resp.status_code != 200:
        raise ModelPullError(
            f"Ollama returned HTTP {resp.status_code} for /api/tags: {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise ModelPullError(f"Ollama /api/tags returned non-JSON: {exc}") from exc

    names: set[str] = set()
    for entry in data.get("models", []) or []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str):
            names.add(name)
            # Ollama always tags as "<name>:<tag>" (e.g. ":latest"). Match
            # bare names too so LLM_MODEL=qwen2.5-coder finds qwen2.5-coder:latest.
            if ":" in name:
                names.add(name.split(":", 1)[0])
    return model in names


def _pull_model(api_base: str, model: str, console: Console, client: httpx.Client) -> None:
    """Stream a pull request to Ollama and surface progress on the console."""
    console.print(f"[yellow]Pulling Ollama model[/] [bold]{model}[/] — this may take a minute on first run...")
    started = time.perf_counter()
    last_status = ""
    last_print = 0.0

    try:
        with client.stream(
            "POST",
            f"{api_base}/api/pull",
            json={"name": model, "stream": True},
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0),
        ) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", errors="replace")
                raise ModelPullError(
                    f"Ollama /api/pull returned HTTP {resp.status_code}: {body[:300]}"
                )
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if "error" in event:
                    raise ModelPullError(f"Ollama pull failed: {event['error']}")
                status = event.get("status", "")
                if status and status != last_status:
                    now = time.perf_counter()
                    # Don't spam the console — at most one line per second.
                    if now - last_print > 1.0 or "success" in status.lower():
                        console.print(f"  [dim]{status}[/]")
                        last_print = now
                    last_status = status
    except httpx.HTTPError as exc:
        raise ModelPullError(f"network error during pull: {exc}") from exc

    elapsed = time.perf_counter() - started
    console.print(f"[green]Model[/] [bold]{model}[/] [green]ready[/] (pulled in {elapsed:.1f}s)")


def ensure_ollama_model(*, console: Console | None = None) -> bool:
    """Make sure the configured Ollama model is locally cached, pulling if not.

    Returns True if a pull was performed, False if the model was already cached.
    Silently does nothing (returns False) when ``LLM_PROVIDER`` is not ``ollama``.
    """
    provider = (os.environ.get("LLM_PROVIDER") or "ollama").strip().lower()
    if provider != "ollama":
        return False

    console = console or Console()

    base_url = os.environ.get("LLM_BASE_URL") or "http://localhost:11434/v1"
    model = os.environ.get("LLM_MODEL") or "qwen2.5-coder:1.5b"
    api_base = _api_base_from_chat_url(base_url)

    with httpx.Client() as client:
        if _model_is_cached(api_base, model, client):
            console.print(
                f"[dim]Ollama model[/] [bold]{model}[/] [dim]already cached, skipping pull[/]"
            )
            return False
        _pull_model(api_base, model, console, client)
        return True
