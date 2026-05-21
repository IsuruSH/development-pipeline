"""Stage 5 — Quality Gates.

Runs four deterministic checks as subprocesses against the generated code:

1. ``ruff check src tests`` — lint
2. ``mypy src``               — static types
3. ``pytest -q tests``        — tests, includes the hand-written sandbox tests
4. ``bandit -q -r src``       — security/policy scan

Each gate's full output is saved to ``audit_logs/<run_id>/gates/<gate>.log``.
The pipeline halts on the *first* failure with a clear cause and a tail of
the offending output.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - subprocesses are tightly scoped & shell=False
import sys
import time
from datetime import UTC, datetime

from .auditor import Auditor
from .observability import PipelineConsole
from .state import GateName, GateResult, PipelineRun, StageName, StageResult, StageStatus

# Order matters: cheap fast gates first.
_GATES: list[tuple[GateName, list[str]]] = [
    (GateName.RUFF, ["ruff", "check", "src", "tests"]),
    (GateName.MYPY, ["mypy", "src"]),
    (GateName.PYTEST, ["pytest", "-q", "tests"]),
    (GateName.BANDIT, ["bandit", "-q", "-r", "src"]),
]


def run(run_state: PipelineRun, auditor: Auditor, console: PipelineConsole) -> PipelineRun:
    stage = StageName.QUALITY_GATES
    console.stage_start(stage, "running ruff / mypy / pytest / bandit")
    started = time.perf_counter()
    result = StageResult(stage=stage, status=StageStatus.RUNNING, started_at=datetime.now(UTC))

    gate_results: list[GateResult] = []
    all_passed = True
    first_failure: str | None = None

    for name, base_cmd in _GATES:
        cmd = _resolve_cmd(base_cmd)
        if cmd is None:
            console.warn(f"{name.value}: executable not found, skipping")
            continue

        console.info(f"running [bold]{' '.join(cmd)}[/]")
        gate_start = time.perf_counter()
        proc = subprocess.run(  # nosec B603 - args list, no shell, fixed tools
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        gate_duration = int((time.perf_counter() - gate_start) * 1000)
        passed = proc.returncode == 0

        log_path = auditor.write_gate_log(name.value, proc.stdout, proc.stderr)
        console.gate_output(name.value, proc.stdout, proc.stderr, passed)

        gate_results.append(
            GateResult(
                gate=name,
                passed=passed,
                duration_ms=gate_duration,
                stdout_tail="\n".join(proc.stdout.splitlines()[-25:]),
                stderr_tail="\n".join(proc.stderr.splitlines()[-25:]),
                log_path=str(log_path),
            )
        )

        auditor.record(
            auditor.event(
                stage,
                f"gate.{'passed' if passed else 'failed'}",
                latency_ms=gate_duration,
                artifact_paths=[str(log_path)],
                payload={"gate": name.value, "returncode": proc.returncode},
                error=None if passed else f"{name.value} returned {proc.returncode}",
            )
        )

        if not passed and first_failure is None:
            all_passed = False
            first_failure = name.value
            # NOTE: spec says "pipeline should fail if required checks do not pass".
            # We continue running the remaining gates so the audit log is complete,
            # but the orchestrator will halt before approval.
            # If you'd rather fail fast, uncomment the `break` below.
            # break

    run_state.gate_results = gate_results
    console.gate_summary(gate_results)

    result.finished_at = datetime.now(UTC)
    result.duration_ms = int((time.perf_counter() - started) * 1000)

    if all_passed and gate_results:
        result.status = StageStatus.PASSED
        result.notes = f"{len(gate_results)} gates passed"
        run_state.record_stage(result)
        auditor.record(
            auditor.event(
                stage,
                "stage.passed",
                latency_ms=result.duration_ms,
                payload={"gate_count": len(gate_results)},
            )
        )
        console.stage_end(stage, StageStatus.PASSED, f"{len(gate_results)} gates green")
    else:
        result.status = StageStatus.FAILED
        result.error = f"gate failed: {first_failure or 'no gates ran'}"
        run_state.record_stage(result)
        run_state.halted = True
        run_state.halt_reason = result.error
        auditor.record(
            auditor.event(
                stage,
                "stage.failed",
                latency_ms=result.duration_ms,
                error=result.error,
            )
        )
        console.stage_end(stage, StageStatus.FAILED, result.error)

    return run_state


def _resolve_cmd(cmd: list[str]) -> list[str] | None:
    """Resolve the first arg to an actual executable, or return None if missing.

    On Windows, the ``ruff`` / ``mypy`` etc. console scripts may live next to
    ``python.exe`` in the venv ``Scripts/`` dir; ``shutil.which`` finds them.
    As a fallback, we run them through ``python -m`` where the module path
    matches the script name.
    """
    tool = cmd[0]
    resolved = shutil.which(tool)
    if resolved:
        return [resolved, *cmd[1:]]
    # Fallback: ``python -m <tool> <args>`` for tools that expose a __main__.
    module_map = {"ruff": "ruff", "mypy": "mypy", "pytest": "pytest", "bandit": "bandit"}
    mod = module_map.get(tool)
    if mod:
        return [sys.executable, "-m", mod, *cmd[1:]]
    return None
