"""LangGraph orchestration of the 7 pipeline stages.

The graph is intentionally simple: each stage is a node that mutates the
``PipelineRun`` state object and returns it. Conditional edges check
``run.halted`` after every stage (to halt cleanly) and the boolean returned
by the approval checkpoints.

A mermaid rendering of the compiled graph is written to the audit directory
so reviewers can see the exact flow that executed.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from langgraph.graph import END, StateGraph

from . import implementer, intake, planner, quality_gates, test_generator
from .approval import request as request_approval
from .auditor import Auditor
from .metrics import compute as compute_metrics
from .metrics import render_summary_md
from .observability import PipelineConsole
from .state import PipelineRun, RejectionFeedback, StageName, StageResult, StageStatus


def _intake_node(run: PipelineRun, *, auditor: Auditor, console: PipelineConsole) -> PipelineRun:
    """Stage 1 — Spec Intake."""
    stage = StageName.INTAKE
    console.stage_start(stage, f"parsing {run.spec_path}")
    started = time.perf_counter()
    result = StageResult(stage=stage, status=StageStatus.RUNNING, started_at=datetime.now(UTC))

    try:
        spec = intake.load_spec(run.spec_path)
    except intake.SpecIntakeError as exc:
        result.status = StageStatus.FAILED
        result.error = str(exc)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        result.finished_at = datetime.now(UTC)
        run.record_stage(result)
        run.halted = True
        run.halt_reason = f"intake: {exc}"
        auditor.record(auditor.event(stage, "stage.failed", error=str(exc)))
        console.stage_end(stage, StageStatus.FAILED, str(exc))
        return run

    run.spec = spec
    snapshot = auditor.snapshot_spec(Path(run.spec_path))

    result.status = StageStatus.PASSED
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    result.finished_at = datetime.now(UTC)
    result.notes = f"feature '{spec.name}', {len(spec.acceptance_criteria)} ACs"
    run.record_stage(result)
    auditor.record(
        auditor.event(
            stage,
            "stage.passed",
            artifact_paths=[str(snapshot)],
            payload={
                "feature": spec.name,
                "ac_count": len(spec.acceptance_criteria),
                "rules": len(spec.business_rules),
            },
        )
    )
    console.stage_end(
        stage, StageStatus.PASSED, f"{len(spec.acceptance_criteria)} acceptance criteria"
    )
    return run


def _approval_plan_node(
    run: PipelineRun, *, auditor: Auditor, console: PipelineConsole
) -> PipelineRun:
    """Stage 6a — Pre-implementation approval.

    On rejection we **do not halt**: the router (:func:`_route_after_approval_plan`)
    sends control back to the planner so the human can iterate on the design.
    The reviewer's note is captured into :attr:`PipelineRun.rejection_feedback`
    and injected into the next planner prompt. We only halt once the cap
    (``run.max_replans``) is exhausted.
    """
    if run.halted:
        return run
    if run.plan is None:
        run.halted = True
        run.halt_reason = "approval_plan: no plan to approve"
        return run
    console.stage_start(StageName.APPROVAL_PLAN, "human approval before implementation")
    summary = (
        f"Plan: {len(run.plan.tasks)} tasks, {len(run.plan.impacted_files)} impacted file(s). "
        f"Design: {run.plan.design_summary[:240]}"
    )
    approved = request_approval(
        run=run,
        auditor=auditor,
        stage=StageName.APPROVAL_PLAN,
        checkpoint="pre-implementation",
        summary=summary,
    )

    if not approved:
        # Capture the human's note for the next planner attempt.
        last = run.approvals[-1] if run.approvals else None
        attempt = run.replan_count + 1  # the attempt that was just rejected
        run.rejection_feedback.append(
            RejectionFeedback(
                checkpoint="pre-implementation",
                actor=last.actor if last else "unknown",
                note=last.note if last else "",
                attempt=attempt,
            )
        )
        run.replan_count += 1

        if run.replan_count > run.max_replans:
            # Out of retries — halt cleanly so finalize still runs.
            run.halted = True
            run.halt_reason = (
                f"plan rejected {run.replan_count} time(s); "
                f"max_replans={run.max_replans} exceeded"
            )
            console.stage_end(
                StageName.APPROVAL_PLAN,
                StageStatus.REJECTED,
                f"rejected (retry cap reached: {run.replan_count}/{run.max_replans})",
            )
            auditor.record(
                auditor.event(
                    StageName.APPROVAL_PLAN,
                    "approval.retries_exhausted",
                    error=run.halt_reason,
                    payload={
                        "replan_count": run.replan_count,
                        "max_replans": run.max_replans,
                    },
                )
            )
        else:
            console.stage_end(
                StageName.APPROVAL_PLAN,
                StageStatus.REJECTED,
                f"rejected — re-planning ({run.replan_count}/{run.max_replans} re-plans used)",
            )
            auditor.record(
                auditor.event(
                    StageName.APPROVAL_PLAN,
                    "approval.replan_requested",
                    payload={
                        "replan_count": run.replan_count,
                        "max_replans": run.max_replans,
                        "note": run.rejection_feedback[-1].note,
                    },
                )
            )
    else:
        console.stage_end(StageName.APPROVAL_PLAN, StageStatus.PASSED, "approved")
    return run


def _approval_deploy_node(
    run: PipelineRun, *, auditor: Auditor, console: PipelineConsole
) -> PipelineRun:
    """Stage 6b — Pre-deployment approval (after gates pass)."""
    if run.halted:
        return run
    console.stage_start(StageName.APPROVAL_DEPLOY, "human approval before deployment")
    summary = (
        f"All quality gates passed. {len(run.generated_files)} src files, "
        f"{len(run.generated_tests)} test files, "
        f"{sum(1 for g in run.gate_results if g.passed)}/{len(run.gate_results)} gates green."
    )
    approved = request_approval(
        run=run,
        auditor=auditor,
        stage=StageName.APPROVAL_DEPLOY,
        checkpoint="pre-deploy",
        summary=summary,
    )
    if not approved:
        run.halted = True
        run.halt_reason = "human rejected deployment"
    console.stage_end(
        StageName.APPROVAL_DEPLOY,
        StageStatus.PASSED if approved else StageStatus.REJECTED,
        "approved" if approved else "rejected",
    )
    return run


def _finalize_node(run: PipelineRun, *, auditor: Auditor, console: PipelineConsole) -> PipelineRun:
    """Stage 7 — Finalisation: compute metrics, write summary, print report."""
    metrics = compute_metrics(run)
    summary_md = render_summary_md(run, metrics)
    auditor.finalize(summary_md)
    console.final_report(run, metrics)
    return run


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_graph(*, auditor: Auditor, console: PipelineConsole):
    """Compile the LangGraph used by :func:`run`."""

    g = StateGraph(PipelineRun)

    g.add_node("intake", lambda s: _intake_node(s, auditor=auditor, console=console))
    g.add_node("planner", lambda s: planner.run(s, auditor, console))
    g.add_node(
        "approval_plan",
        lambda s: _approval_plan_node(s, auditor=auditor, console=console),
    )
    g.add_node("implementer", lambda s: implementer.run(s, auditor, console))
    g.add_node("test_generator", lambda s: test_generator.run(s, auditor, console))
    g.add_node("quality_gates", lambda s: quality_gates.run(s, auditor, console))
    g.add_node(
        "approval_deploy",
        lambda s: _approval_deploy_node(s, auditor=auditor, console=console),
    )
    g.add_node("finalize", lambda s: _finalize_node(s, auditor=auditor, console=console))

    g.set_entry_point("intake")

    def cont_or_halt(next_node: str):
        def _router(state: PipelineRun) -> str:
            return "finalize" if state.halted else next_node

        return _router

    def _route_after_approval_plan(state: PipelineRun) -> str:
        """Route after the pre-implementation approval checkpoint.

        - halted (incl. retry cap exhausted) -> finalize
        - last approval was rejected (and we still have retries) -> planner
        - approved -> implementer
        """
        if state.halted:
            return "finalize"
        last = state.approvals[-1] if state.approvals else None
        if last is not None and last.decision == "rejected":
            return "planner"
        return "implementer"

    g.add_conditional_edges("intake", cont_or_halt("planner"), {"planner": "planner", "finalize": "finalize"})
    g.add_conditional_edges("planner", cont_or_halt("approval_plan"), {"approval_plan": "approval_plan", "finalize": "finalize"})
    g.add_conditional_edges(
        "approval_plan",
        _route_after_approval_plan,
        {"planner": "planner", "implementer": "implementer", "finalize": "finalize"},
    )
    g.add_conditional_edges("implementer", cont_or_halt("test_generator"), {"test_generator": "test_generator", "finalize": "finalize"})
    g.add_conditional_edges("test_generator", cont_or_halt("quality_gates"), {"quality_gates": "quality_gates", "finalize": "finalize"})
    g.add_conditional_edges("quality_gates", cont_or_halt("approval_deploy"), {"approval_deploy": "approval_deploy", "finalize": "finalize"})
    g.add_edge("approval_deploy", "finalize")
    g.add_edge("finalize", END)

    return g.compile()


def export_mermaid(compiled_graph, auditor: Auditor) -> None:
    """Persist a mermaid diagram of the compiled graph for audit."""
    try:
        mermaid = compiled_graph.get_graph().draw_mermaid()
    except Exception as exc:  # pragma: no cover - depends on langgraph version
        mermaid = f"%% failed to render mermaid: {exc}"
    auditor.write_graph(mermaid)


def run(
    *,
    spec_path: str,
    auto_approve: bool = False,
    verbose: bool = False,
    model: str = "auto",
) -> PipelineRun:
    """Top-level entry point used by ``run_pipeline.py``."""
    from .ensure_model import ModelPullError, ensure_ollama_model
    from .llm import default_model

    resolved_model = default_model() if model in ("", "auto") else model
    initial = PipelineRun(
        spec_path=spec_path,
        auto_approve=auto_approve,
        verbose=verbose,
        model=resolved_model,
    )
    console = PipelineConsole(verbose=verbose)
    auditor = Auditor(initial)
    console.banner(initial)

    # When running against Ollama, make sure the model is locally cached
    # before we kick off the graph. This used to be a docker-compose
    # `ollama-pull` sidecar but its `condition: service_completed_successfully`
    # dependency hangs in Compose v5.x; doing it here is more robust.
    try:
        ensure_ollama_model(console=console.console)
    except ModelPullError as exc:
        initial.halted = True
        initial.halt_reason = f"ensure_model: {exc}"
        console.console.print(f"[bold red]Could not prepare model:[/] {exc}")
        return initial

    graph = build_graph(auditor=auditor, console=console)
    export_mermaid(graph, auditor)

    final_state = graph.invoke(initial)

    # ``graph.invoke`` returns a dict-like view of the state in some langgraph
    # versions; normalise back to the Pydantic model so callers always get one.
    if isinstance(final_state, PipelineRun):
        return final_state
    return PipelineRun.model_validate(final_state)
