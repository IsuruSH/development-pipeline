"""Auditor — append-only JSONL trail + run-summary writer.

Every stage calls :meth:`Auditor.record` exactly once before returning. The
single source of truth on disk is ``audit_logs/<run_id>/audit.jsonl``; the
other files (``plan.json``, ``prompts/...``, ``responses/...``,
``gates/...``, ``summary.md``) are *derivatives* the orchestrator writes for
human review, but the JSONL alone is sufficient to reproduce a run.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .state import AuditEvent, PipelineRun, StageName

AUDIT_ROOT = Path("audit_logs")


class Auditor:
    """Per-run append-only logger."""

    def __init__(self, run: PipelineRun, root: Path = AUDIT_ROOT) -> None:
        self.run = run
        self.dir = root / run.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "prompts").mkdir(exist_ok=True)
        (self.dir / "responses").mkdir(exist_ok=True)
        (self.dir / "gates").mkdir(exist_ok=True)
        self.jsonl_path = self.dir / "audit.jsonl"
        run.audit_dir = self.dir

    # ------------------------------------------------------------------
    # Core record API
    # ------------------------------------------------------------------

    def record(self, event: AuditEvent) -> None:
        """Append a single audit event to ``audit.jsonl``."""
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")

    def event(self, stage: StageName, event_type: str, **kwargs: Any) -> AuditEvent:
        """Convenience factory that auto-fills ``run_id``."""
        return AuditEvent(run_id=self.run.run_id, stage=stage, event_type=event_type, **kwargs)

    # ------------------------------------------------------------------
    # Snapshot helpers (called by orchestrator at well-defined points)
    # ------------------------------------------------------------------

    def snapshot_spec(self, spec_source_path: Path) -> Path:
        """Copy the original spec file verbatim for reproducibility."""
        dest = self.dir / f"spec.snapshot{spec_source_path.suffix}"
        shutil.copyfile(spec_source_path, dest)
        return dest

    def write_prompt(
        self, stage: StageName, version: str, text: str, *, attempt: int = 1
    ) -> Path:
        # First attempt keeps the original filename; subsequent attempts (e.g.
        # planner re-runs after a rejection) get an .attempt-N suffix so prior
        # tries aren't overwritten.
        suffix = "" if attempt <= 1 else f".attempt-{attempt}"
        path = self.dir / "prompts" / f"{stage.value}.{version}{suffix}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def write_response(
        self, stage: StageName, raw_text: str, *, attempt: int = 1
    ) -> Path:
        suffix = "" if attempt <= 1 else f".attempt-{attempt}"
        path = self.dir / "responses" / f"{stage.value}{suffix}.raw.json"
        path.write_text(raw_text, encoding="utf-8")
        return path

    def write_plan(self, plan_obj: dict[str, Any]) -> Path:
        path = self.dir / "plan.json"
        path.write_text(json.dumps(plan_obj, indent=2), encoding="utf-8")
        return path

    def write_changes_summary(self, markdown: str) -> Path:
        path = self.dir / "changes_summary.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    def write_gate_log(self, gate: str, stdout: str, stderr: str) -> Path:
        path = self.dir / "gates" / f"{gate}.log"
        path.write_text(
            f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n",
            encoding="utf-8",
        )
        return path

    def write_graph(self, mermaid: str, png_bytes: bytes | None = None) -> tuple[Path, Path | None]:
        md_path = self.dir / "graph.mmd"
        md_path.write_text(mermaid, encoding="utf-8")
        png_path = None
        if png_bytes:
            png_path = self.dir / "graph.png"
            png_path.write_bytes(png_bytes)
        return md_path, png_path

    def write_summary(self, markdown: str) -> Path:
        path = self.dir / "summary.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # Finalisation
    # ------------------------------------------------------------------

    def finalize(self, summary_md: str) -> Path:
        """Write the summary file and record a final ``run_finalized`` event."""
        summary_path = self.write_summary(summary_md)
        self.record(
            self.event(
                StageName.FINALIZE,
                "run_finalized",
                artifact_paths=[str(summary_path)],
                payload={"finished_at": datetime.now(UTC).isoformat()},
            )
        )
        return summary_path
