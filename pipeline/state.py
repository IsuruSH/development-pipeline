"""Typed state passed between LangGraph stages.

All models are :mod:`pydantic` v2 ``BaseModel`` subclasses so the state can be
serialised to JSON for the audit trail and validated with precise error
messages on intake.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


class AcceptanceCriterion(BaseModel):
    """A single acceptance criterion.

    The ``id`` field is intentionally stable (``AC-1``, ``AC-2`` ...) because
    generated tests reference it via a ``Covers: AC-<n>`` docstring tag, and
    the metrics module greps for those tags to compute coverage.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^AC-\d+$", description="Stable identifier, e.g. 'AC-1'.")
    description: str = Field(min_length=1)


class FeatureSpec(BaseModel):
    """The structured feature specification ingested at stage 1."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Short feature name; used for paths/logs.")
    objective: str = Field(min_length=1)
    user_story: str = Field(min_length=1)
    business_rules: list[str] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    non_functional_requirements: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)

    @field_validator("acceptance_criteria")
    @classmethod
    def _unique_ac_ids(
        cls, v: list[AcceptanceCriterion]
    ) -> list[AcceptanceCriterion]:
        ids = [ac.id for ac in v]
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance_criteria ids must be unique")
        return v


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class PlanTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    description: str
    covers_acceptance_criteria: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    """Structured plan returned by the planner stage."""

    model_config = ConfigDict(extra="ignore")

    design_summary: str
    tasks: list[PlanTask]
    impacted_files: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    test_strategy: str = ""


# ---------------------------------------------------------------------------
# Implementation / generated artefacts
# ---------------------------------------------------------------------------


class GeneratedFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(description="Relative path (within sandbox root) of the file.")
    content: str
    summary: str = ""


# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------


class GateName(StrEnum):
    RUFF = "ruff"
    MYPY = "mypy"
    PYTEST = "pytest"
    BANDIT = "bandit"


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: GateName
    passed: bool
    duration_ms: int
    stdout_tail: str = ""
    stderr_tail: str = ""
    log_path: str | None = None


# ---------------------------------------------------------------------------
# Stage results & audit events
# ---------------------------------------------------------------------------


class StageName(StrEnum):
    INTAKE = "intake"
    PLANNER = "planner"
    APPROVAL_PLAN = "approval_plan"
    IMPLEMENTER = "implementer"
    TEST_GENERATOR = "test_generator"
    QUALITY_GATES = "quality_gates"
    APPROVAL_DEPLOY = "approval_deploy"
    FINALIZE = "finalize"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: StageName
    status: StageStatus = StageStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    notes: str = ""


class AuditEvent(BaseModel):
    """One append-only record in ``audit.jsonl``."""

    model_config = ConfigDict(extra="ignore")

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str
    stage: StageName
    event_type: str
    prompt_version: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    decision: str | None = None
    artifact_paths: list[str] = Field(default_factory=list)
    error: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Top-level pipeline run state
# ---------------------------------------------------------------------------


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint: str
    actor: str
    decision: str  # "approved" | "rejected"
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    note: str = ""


class RejectionFeedback(BaseModel):
    """Captured when a human rejects an approval checkpoint.

    Used by the planner on subsequent re-plan attempts so the model can
    address the reviewer's concerns instead of regenerating the same plan.
    """

    model_config = ConfigDict(extra="forbid")

    checkpoint: str
    actor: str
    note: str
    attempt: int  # which planner attempt (1-indexed) was rejected
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PipelineRun(BaseModel):
    """Mutable state object threaded through every LangGraph node."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str = Field(default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6])
    spec_path: str
    spec: FeatureSpec | None = None
    plan: Plan | None = None
    generated_files: list[GeneratedFile] = Field(default_factory=list)
    generated_tests: list[GeneratedFile] = Field(default_factory=list)
    gate_results: list[GateResult] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    rejection_feedback: list[RejectionFeedback] = Field(default_factory=list)
    stage_results: dict[StageName, StageResult] = Field(default_factory=dict)
    halted: bool = False
    halt_reason: str | None = None
    auto_approve: bool = False
    verbose: bool = False
    # Model id is resolved per-provider in pipeline.llm if left as "auto".
    model: str = "auto"
    # Re-plan loop: how many times the planner has been re-invoked because a
    # human rejected the plan at approval_plan, and the cap before we halt.
    replan_count: int = 0
    max_replans: int = 3
    audit_dir: Path | None = None

    def audit_root(self) -> Path:
        if self.audit_dir is None:
            raise RuntimeError("audit_dir not yet initialised")
        return self.audit_dir

    def record_stage(self, result: StageResult) -> None:
        self.stage_results[result.stage] = result
