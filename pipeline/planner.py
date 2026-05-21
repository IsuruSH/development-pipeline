"""Stage 2 — Planning Layer.

Sends the spec to Claude with a structured prompt and parses the response
into a typed :class:`Plan`. Saves both the exact prompt and the parsed plan
to the audit directory.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from pydantic import ValidationError

from .auditor import Auditor
from .llm import LLMError, call_json
from .observability import PipelineConsole
from .prompts import registry
from .state import PipelineRun, Plan, RejectionFeedback, StageName, StageResult, StageStatus


def _build_feedback_block(feedback: list[RejectionFeedback]) -> str:
    """Format prior-rejection notes for injection into the planner prompt.

    Empty when there is no feedback yet (first plan attempt) — that way the
    prompt is byte-identical to the no-feedback baseline.
    """
    if not feedback:
        return ""
    bullets: list[str] = []
    for fb in feedback:
        note = fb.note.strip() or "(no reason given)"
        bullets.append(f"- [attempt {fb.attempt}, by {fb.actor}] {note}")
    body = "\n".join(bullets)
    return (
        "\n## Prior rejection feedback\n\n"
        "A human reviewer rejected previous plan attempt(s). Your new plan "
        "MUST address these concerns:\n\n"
        f"{body}\n"
    )


def run(run_state: PipelineRun, auditor: Auditor, console: PipelineConsole) -> PipelineRun:
    stage = StageName.PLANNER
    attempt = run_state.replan_count + 1  # 1-indexed: first attempt is 1
    if attempt == 1:
        note = "asking Claude for a structured plan"
    else:
        note = (
            f"re-planning (attempt {attempt}/{run_state.max_replans + 1}) "
            "based on reviewer feedback"
        )
    console.stage_start(stage, note)
    started = time.perf_counter()
    result = StageResult(stage=stage, status=StageStatus.RUNNING, started_at=datetime.now(UTC))

    if run_state.spec is None:
        raise RuntimeError("planner stage requires run_state.spec to be set")

    template = registry.get("planner")
    spec_json = run_state.spec.model_dump_json(indent=2)
    feedback_block = _build_feedback_block(run_state.rejection_feedback)
    prompt = template.render(spec_json=spec_json, rejection_feedback=feedback_block)

    auditor.write_prompt(stage, template.version, prompt, attempt=attempt)
    console.prompt(stage, template.version, prompt)

    try:
        parsed, usage = call_json(prompt=prompt, model=run_state.model, max_tokens=4096)
    except LLMError as exc:
        result.status = StageStatus.FAILED
        result.error = str(exc)
        result.finished_at = datetime.now(UTC)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        run_state.record_stage(result)
        run_state.halted = True
        run_state.halt_reason = f"planner: {exc}"
        auditor.record(auditor.event(stage, "stage.failed", error=str(exc)))
        console.stage_end(stage, StageStatus.FAILED, str(exc))
        return run_state

    auditor.write_response(stage, usage.raw_text, attempt=attempt)
    console.response(stage, usage.raw_text)

    try:
        plan = Plan.model_validate(parsed)
    except ValidationError as exc:
        result.status = StageStatus.FAILED
        result.error = f"plan schema invalid: {exc}"
        result.finished_at = datetime.now(UTC)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        result.tokens_in = usage.tokens_in
        result.tokens_out = usage.tokens_out
        result.cost_usd = usage.cost_usd
        run_state.record_stage(result)
        run_state.halted = True
        run_state.halt_reason = "planner returned an invalid plan"
        auditor.record(auditor.event(stage, "stage.failed", error=str(exc)))
        console.stage_end(stage, StageStatus.FAILED, "invalid plan schema")
        return run_state

    plan_path = auditor.write_plan(plan.model_dump())
    run_state.plan = plan

    result.status = StageStatus.PASSED
    result.finished_at = datetime.now(UTC)
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    result.tokens_in = usage.tokens_in
    result.tokens_out = usage.tokens_out
    result.cost_usd = usage.cost_usd
    result.notes = f"{len(plan.tasks)} tasks, {len(plan.impacted_files)} impacted files"
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
            artifact_paths=[str(plan_path)],
            payload={
                "task_count": len(plan.tasks),
                "impacted_files": plan.impacted_files,
                "attempt": attempt,
                "had_rejection_feedback": bool(run_state.rejection_feedback),
            },
        )
    )

    console.stage_end(
        stage,
        StageStatus.PASSED,
        f"{len(plan.tasks)} tasks · {usage.tokens_in}/{usage.tokens_out} tokens · ${usage.cost_usd:.4f}",
    )
    return run_state
