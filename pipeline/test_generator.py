"""Stage 4 — Automated Test Generation.

Sends the spec and the freshly generated source files to Claude, parses the
returned ``tests/test_*.py`` files, sandboxes their writes, and stores them
under ``tests/``. Tests for each acceptance criterion include a
``Covers: AC-<n>`` tag which :mod:`pipeline.metrics` greps for to compute
coverage.
"""

from __future__ import annotations

import json
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

TESTS_ROOT = Path("tests")


def run(run_state: PipelineRun, auditor: Auditor, console: PipelineConsole) -> PipelineRun:
    stage = StageName.TEST_GENERATOR
    console.stage_start(stage, "asking Claude to generate pytest tests")
    started = time.perf_counter()
    result = StageResult(stage=stage, status=StageStatus.RUNNING, started_at=datetime.now(UTC))

    if run_state.spec is None:
        raise RuntimeError("test_generator stage requires spec to be set")
    if not run_state.generated_files:
        return _fail(run_state, auditor, console, result, started, stage, "no generated source files to test")

    template = registry.get("test_generator")
    source_payload = [
        {"path": f.path, "content": f.content}
        for f in run_state.generated_files
    ]
    prompt = template.render(
        spec_json=run_state.spec.model_dump_json(indent=2),
        source_files_json=json.dumps(source_payload, indent=2),
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
            "test generator returned no files",
            usage=usage,
        )

    TESTS_ROOT.mkdir(parents=True, exist_ok=True)
    sandbox_root = TESTS_ROOT.resolve()

    written: list[GeneratedFile] = []
    try:
        for entry in files_raw:
            path = entry.get("path", "")
            content = entry.get("content", "")
            summary = entry.get("summary", "")

            if not isinstance(path, str) or not isinstance(content, str):
                raise ValueError(f"invalid file entry: {entry!r}")

            normalised = path.replace("\\", "/")
            if not normalised.startswith("tests/"):
                raise SandboxViolation(
                    f"Test generator tried to write outside tests/: {normalised!r}"
                )

            relative = normalised[len("tests/") :]

            # Refuse to overwrite our hand-written pipeline tests.
            if relative.startswith("pipeline_"):
                raise SandboxViolation(
                    f"Refusing to overwrite reserved pipeline test file: {normalised!r}"
                )

            sandbox_write(relative, content, allowed_root=sandbox_root)
            written.append(GeneratedFile(path=normalised, content=content, summary=summary))
    except (SandboxViolation, ValueError) as exc:
        return _fail(run_state, auditor, console, result, started, stage, str(exc), usage=usage)

    run_state.generated_tests = written

    result.status = StageStatus.PASSED
    result.finished_at = datetime.now(UTC)
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    result.tokens_in = usage.tokens_in
    result.tokens_out = usage.tokens_out
    result.cost_usd = usage.cost_usd
    result.notes = f"{len(written)} test files written under tests/"
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
            artifact_paths=[f.path for f in written],
            payload={"file_count": len(written)},
        )
    )

    console.stage_end(
        stage,
        StageStatus.PASSED,
        f"{len(written)} test files · {usage.tokens_in}/{usage.tokens_out} tokens · ${usage.cost_usd:.4f}",
    )
    for f in written:
        console.info(f"wrote [bold]{f.path}[/]  [dim]{f.summary}[/]")
    return run_state


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
