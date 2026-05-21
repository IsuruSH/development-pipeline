"""Per-run evaluation metrics.

Computed at the very end of a run and rendered into ``summary.md`` and the
final Rich CLI table.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .state import GeneratedFile, PipelineRun, StageName

_AC_COVERS_RE = re.compile(r"Covers:\s*(AC-\d+)")


@dataclass
class RunMetrics:
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    per_stage_duration_ms: dict[str, int] = field(default_factory=dict)
    per_stage_tokens: dict[str, tuple[int, int]] = field(default_factory=dict)
    files_generated: int = 0
    lines_generated: int = 0
    tests_generated: int = 0
    test_lines_generated: int = 0
    ac_total: int = 0
    ac_covered: int = 0
    ac_coverage_pct: float = 0.0
    gates_run: int = 0
    gates_passed: int = 0
    gate_pass_rate: float = 0.0
    halted: bool = False
    halt_reason: str | None = None


def compute(run: PipelineRun) -> RunMetrics:
    m = RunMetrics()

    for stage_name, result in run.stage_results.items():
        m.total_tokens_in += result.tokens_in
        m.total_tokens_out += result.tokens_out
        m.total_cost_usd += result.cost_usd
        m.total_duration_ms += result.duration_ms
        m.per_stage_duration_ms[stage_name.value] = result.duration_ms
        m.per_stage_tokens[stage_name.value] = (result.tokens_in, result.tokens_out)

    m.files_generated = len(run.generated_files)
    m.lines_generated = _count_lines(run.generated_files)
    m.tests_generated = len(run.generated_tests)
    m.test_lines_generated = _count_lines(run.generated_tests)

    if run.spec is not None:
        m.ac_total = len(run.spec.acceptance_criteria)
        covered = _covered_acs(run.generated_tests)
        ac_ids = {ac.id for ac in run.spec.acceptance_criteria}
        m.ac_covered = len(covered & ac_ids)
        m.ac_coverage_pct = (m.ac_covered / m.ac_total * 100.0) if m.ac_total else 0.0

    m.gates_run = len(run.gate_results)
    m.gates_passed = sum(1 for g in run.gate_results if g.passed)
    m.gate_pass_rate = (m.gates_passed / m.gates_run * 100.0) if m.gates_run else 0.0

    m.halted = run.halted
    m.halt_reason = run.halt_reason

    return m


def _count_lines(files: Iterable[GeneratedFile]) -> int:
    return sum(len(f.content.splitlines()) for f in files)


def _covered_acs(test_files: Iterable[GeneratedFile]) -> set[str]:
    found: set[str] = set()
    for f in test_files:
        for match in _AC_COVERS_RE.finditer(f.content):
            found.add(match.group(1))
    return found


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


def render_summary_md(run: PipelineRun, m: RunMetrics) -> str:
    spec_name = run.spec.name if run.spec else "<unknown>"
    lines = [
        f"# Run summary — `{run.run_id}`",
        "",
        f"- **Feature:** {spec_name}",
        f"- **Status:** {'HALTED' if m.halted else 'COMPLETED'}",
    ]
    if m.halted and m.halt_reason:
        lines.append(f"- **Halt reason:** {m.halt_reason}")

    lines += [
        "",
        "## Totals",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Total duration | {m.total_duration_ms} ms |",
        f"| Tokens in / out | {m.total_tokens_in:,} / {m.total_tokens_out:,} |",
        f"| Estimated cost | ${m.total_cost_usd:.4f} |",
        f"| Files generated | {m.files_generated} ({m.lines_generated} lines) |",
        f"| Tests generated | {m.tests_generated} ({m.test_lines_generated} lines) |",
        f"| Acceptance coverage | {m.ac_covered}/{m.ac_total} ({m.ac_coverage_pct:.1f}%) |",
        f"| Quality gates passed | {m.gates_passed}/{m.gates_run} ({m.gate_pass_rate:.1f}%) |",
        "",
        "## Per-stage timing",
        "",
        "| Stage | Status | Duration (ms) | Tokens in/out |",
        "| --- | --- | --- | --- |",
    ]
    for stage in StageName:
        result = run.stage_results.get(stage)
        if result is None:
            continue
        tokens_in, tokens_out = m.per_stage_tokens.get(stage.value, (0, 0))
        lines.append(
            f"| {stage.value} | {result.status.value} | {result.duration_ms} | {tokens_in}/{tokens_out} |"
        )

    if run.gate_results:
        lines += [
            "",
            "## Quality gates",
            "",
            "| Gate | Status | Duration (ms) | Log |",
            "| --- | --- | --- | --- |",
        ]
        for g in run.gate_results:
            status_word = "PASS" if g.passed else "FAIL"
            log_ref = g.log_path or "-"
            lines.append(f"| {g.gate.value} | {status_word} | {g.duration_ms} | {log_ref} |")

    if run.approvals:
        lines += ["", "## Approvals", ""]
        for a in run.approvals:
            lines.append(
                f"- **{a.checkpoint}** by `{a.actor}` at {a.ts.isoformat()} → **{a.decision.upper()}**"
                + (f" — {a.note}" if a.note else "")
            )

    if run.spec:
        lines += ["", "## Acceptance criteria coverage", ""]
        covered = _covered_acs(run.generated_tests)
        for ac in run.spec.acceptance_criteria:
            mark = "x" if ac.id in covered else " "
            lines.append(f"- [{mark}] **{ac.id}** — {ac.description}")

    lines.append("")
    return "\n".join(lines)
