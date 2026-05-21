"""Human-in-the-loop approval checkpoints.

Two checkpoints are used:

- ``approval_plan``  — after the Planner stage, before any code is written.
- ``approval_deploy`` — after Quality Gates pass, before "deployment".

When the pipeline is invoked with ``--auto-approve`` (or
``PIPELINE_AUTO_APPROVE=1``) both checkpoints automatically record an
"approved" decision by the configured actor — this is what CI uses.
"""

from __future__ import annotations

import getpass
import os

from rich.prompt import Confirm, Prompt

from .auditor import Auditor
from .state import Approval, PipelineRun, StageName, StageResult, StageStatus


def _default_actor() -> str:
    return os.environ.get("PIPELINE_ACTOR") or getpass.getuser() or "anonymous"


def _auto_approve_env() -> bool:
    return os.environ.get("PIPELINE_AUTO_APPROVE", "0") in {"1", "true", "TRUE", "yes"}


def request(
    *,
    run: PipelineRun,
    auditor: Auditor,
    stage: StageName,
    checkpoint: str,
    summary: str,
) -> bool:
    """Block on a human approval (or auto-approve) and record the decision.

    Returns ``True`` if approved, ``False`` if rejected. The caller is
    responsible for halting the pipeline on rejection.
    """

    actor = _default_actor()
    auto = run.auto_approve or _auto_approve_env()

    if auto:
        decision = "approved"
        note = "auto-approved (CI / --auto-approve)"
        approved = True
    else:
        approved = Confirm.ask(f"[bold]{summary}[/]\nApprove and continue?", default=True)
        decision = "approved" if approved else "rejected"
        if approved:
            note = ""
        else:
            # On rejection, capture an optional reason. For approval_plan this
            # becomes RejectionFeedback that the planner sees on re-plan; for
            # other checkpoints it's just preserved in the audit log.
            note = Prompt.ask(
                "[yellow]Reason for rejection (optional, but helps the planner)[/]",
                default="",
            ).strip()

    record = Approval(checkpoint=checkpoint, actor=actor, decision=decision, note=note)
    run.approvals.append(record)

    auditor.record(
        auditor.event(
            stage,
            event_type=f"approval.{decision}",
            decision=decision,
            payload={
                "checkpoint": checkpoint,
                "actor": actor,
                "note": note,
                "summary": summary,
            },
        )
    )

    run.record_stage(
        StageResult(
            stage=stage,
            status=StageStatus.PASSED if approved else StageStatus.REJECTED,
            notes=f"{checkpoint} by {actor}: {decision}",
        )
    )
    return approved
