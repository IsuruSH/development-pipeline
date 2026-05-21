"""CLI entry point for the spec-driven development pipeline.

Usage:

    # Default (local Ollama via docker-compose):
    python run_pipeline.py specs/user_authentication.yaml

    # Non-interactive (CI / smoke run):
    python run_pipeline.py specs/user_authentication.yaml --auto-approve --verbose

    # Pick a different provider / model from the command line:
    python run_pipeline.py specs/user_authentication.yaml --provider openai --model gpt-4o-mini
    python run_pipeline.py specs/user_authentication.yaml --provider anthropic --model claude-sonnet-4-5

The provider, model, base URL, and API key can also all be set via the
LLM_PROVIDER / LLM_MODEL / LLM_BASE_URL / LLM_API_KEY environment variables
(typically in a .env file) — see .env.example.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console

from pipeline.graph import run as run_pipeline

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)


@app.command()
def main(
    spec: Path = typer.Argument(
        ...,
        exists=True,
        readable=True,
        help="Path to the feature spec file (.yaml/.yml/.json/.md).",
    ),
    auto_approve: bool = typer.Option(
        False,
        "--auto-approve",
        help="Skip interactive approval prompts (intended for CI).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Print full prompts and model responses for each stage.",
    ),
    provider: str = typer.Option(
        None,
        "--provider",
        envvar="LLM_PROVIDER",
        help="LLM provider: 'ollama' (default), 'openai', or 'anthropic'.",
    ),
    model: str = typer.Option(
        "auto",
        "--model",
        envvar="LLM_MODEL",
        help="Model id. Use 'auto' (default) to pick a sensible per-provider default.",
    ),
    base_url: str = typer.Option(
        None,
        "--base-url",
        envvar="LLM_BASE_URL",
        help="Override the provider base URL (e.g. http://ollama:11434/v1).",
    ),
) -> None:
    """Execute the spec-driven pipeline end-to-end on ``spec``."""
    load_dotenv()

    # Echo the CLI overrides into the env so the LLM layer (which reads env)
    # sees the same values. Typer already pulled them from env if not on CLI.
    if provider:
        os.environ["LLM_PROVIDER"] = provider
    if base_url:
        os.environ["LLM_BASE_URL"] = base_url
    if model and model != "auto":
        os.environ["LLM_MODEL"] = model

    console = Console()
    try:
        final = run_pipeline(
            spec_path=str(spec),
            auto_approve=auto_approve,
            verbose=verbose,
            model=model,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Pipeline interrupted by user.[/]")
        raise typer.Exit(code=130) from None
    except Exception as exc:  # noqa: BLE001 - top-level boundary
        console.print(f"[bold red]Pipeline crashed:[/] {exc}")
        raise typer.Exit(code=2) from exc

    if final.halted:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    app()
