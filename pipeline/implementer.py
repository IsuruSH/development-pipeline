"""Stage 3 — AI-assisted Implementation.

Sends the spec + approved plan to Claude, parses the returned file list, runs
every path through the sandbox guard, and writes the files to disk. Produces
a ``changes_summary.md`` describing what was generated.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from .auditor import Auditor
from .llm import LLMError, call_json
from .observability import PipelineConsole
from .prompts import registry
from .sandbox import SandboxViolation
from .sandbox import write as sandbox_write
from .state import GeneratedFile, PipelineRun, StageName, StageResult, StageStatus

# The implementer is only allowed to write into this directory tree.
SRC_ROOT = Path("src")


def run(run_state: PipelineRun, auditor: Auditor, console: PipelineConsole) -> PipelineRun:
    stage = StageName.IMPLEMENTER
    console.stage_start(stage, "asking Claude to generate code")
    started = time.perf_counter()
    result = StageResult(stage=stage, status=StageStatus.RUNNING, started_at=datetime.now(UTC))

    if run_state.spec is None or run_state.plan is None:
        raise RuntimeError("implementer stage requires spec and plan to be set")

    template = registry.get("implementer")
    prompt = template.render(
        spec_json=run_state.spec.model_dump_json(indent=2),
        plan_json=run_state.plan.model_dump_json(indent=2),
    )
    auditor.write_prompt(stage, template.version, prompt)
    console.prompt(stage, template.version, prompt)

    try:
        parsed, usage = call_json(prompt=prompt, model=run_state.model, max_tokens=8192)
    except LLMError as exc:
        return _fail(run_state, auditor, console, result, started, stage, str(exc))

    auditor.write_response(stage, usage.raw_text)
    console.response(stage, usage.raw_text)

    files_raw = parsed.get("files", [])
    if not isinstance(files_raw, list) or not files_raw:
        return _fail(
            run_state,
            auditor,
            console,
            result,
            started,
            stage,
            "implementer returned no files",
            usage=usage,
        )

    SRC_ROOT.mkdir(parents=True, exist_ok=True)
    sandbox_root = SRC_ROOT.resolve()

    written: list[GeneratedFile] = []
    try:
        for entry in files_raw:
            path = entry.get("path", "")
            content = entry.get("content", "")
            summary = entry.get("summary", "")

            if not isinstance(path, str) or not isinstance(content, str):
                raise ValueError(f"invalid file entry: {entry!r}")

            # Strip leading "src/" so the path becomes relative to sandbox_root.
            normalised = path.replace("\\", "/")
            if not normalised.startswith("src/"):
                raise SandboxViolation(
                    f"Implementer tried to write outside src/: {normalised!r}"
                )
            relative = normalised[len("src/") :]

            sandbox_write(relative, content, allowed_root=sandbox_root)
            written.append(GeneratedFile(path=normalised, content=content, summary=summary))
    except (SandboxViolation, ValueError) as exc:
        return _fail(run_state, auditor, console, result, started, stage, str(exc), usage=usage)

    run_state.generated_files = written

    changes_md = _render_changes_summary(written, parsed.get("changes_summary", ""))
    changes_path = auditor.write_changes_summary(changes_md)

    result.status = StageStatus.PASSED
    result.finished_at = datetime.now(UTC)
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    result.tokens_in = usage.tokens_in
    result.tokens_out = usage.tokens_out
    result.cost_usd = usage.cost_usd
    result.notes = f"{len(written)} files written under src/"
    run_state.record_stage(result)

    auditor.record(
        auditor.event(
            stage,
            "stage.passed",
            prompt_version=template.version,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cost_usd=usage.cost_usd,
            latency_ms=usage.latency_ms,
            artifact_paths=[f.path for f in written] + [str(changes_path)],
            payload={"file_count": len(written)},
        )
    )

    console.stage_end(
        stage,
        StageStatus.PASSED,
        f"{len(written)} files · {usage.tokens_in}/{usage.tokens_out} tokens · ${usage.cost_usd:.4f}",
    )
    for f in written:
        console.info(f"wrote [bold]{f.path}[/]  [dim]{f.summary}[/]")
    return run_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fail(
    run_state: PipelineRun,
    auditor: Auditor,
    console: PipelineConsole,
    result: StageResult,
    started: float,
    stage: StageName,
    msg: str,
    *,
    usage=None,
) -> PipelineRun:
    result.status = StageStatus.FAILED
    result.error = msg
    result.finished_at = datetime.now(UTC)
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    if usage is not None:
        result.tokens_in = usage.tokens_in
        result.tokens_out = usage.tokens_out
        result.cost_usd = usage.cost_usd
    run_state.record_stage(result)
    run_state.halted = True
    run_state.halt_reason = f"{stage.value}: {msg}"
    auditor.record(auditor.event(stage, "stage.failed", error=msg))
    console.stage_end(stage, StageStatus.FAILED, msg)
    return run_state


def _render_changes_summary(files: list[GeneratedFile], model_summary: str) -> str:
    lines = ["# Changes summary", ""]
    if model_summary:
        lines += [model_summary.strip(), ""]
    lines += ["## Files", ""]
    for f in files:
        lines.append(f"- `{f.path}` — {f.summary or 'generated'}")
    lines.append("")
    return "\n".join(lines)


__all__ = ["run", "SRC_ROOT"]
