"""Rich-based CLI observability layer.

A single :class:`PipelineConsole` is shared by every stage. It prints:

- A banner at startup with the run id and spec name.
- A live stage table updated as stages start / finish.
- (Verbose mode) syntax-highlighted prompt and response panels.
- A final metrics table and the file tree of the produced audit directory.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .metrics import RunMetrics
from .state import GateResult, PipelineRun, StageName, StageStatus

_STAGE_ORDER: list[StageName] = list(StageName)


class PipelineConsole:
    def __init__(self, *, verbose: bool = False, console: Console | None = None) -> None:
        self.verbose = verbose
        self.console = console or Console()

    # ------------------------------------------------------------------
    # Banners / lifecycle
    # ------------------------------------------------------------------

    def banner(self, run: PipelineRun) -> None:
        import os

        spec_name = run.spec.name if run.spec else Path(run.spec_path).stem
        provider = os.environ.get("LLM_PROVIDER", "ollama")
        base_url = os.environ.get("LLM_BASE_URL", "<provider default>")
        body = Text()
        body.append("Spec-Driven Development Pipeline\n", style="bold cyan")
        body.append(f"run_id   : {run.run_id}\n")
        body.append(f"spec     : {spec_name}  ({run.spec_path})\n")
        body.append(f"provider : {provider}\n")
        body.append(f"model    : {run.model}\n")
        body.append(f"base_url : {base_url}\n")
        body.append(f"auto-approve : {run.auto_approve}\n")
        body.append(f"audit    : {run.audit_dir}\n")
        self.console.print(Panel(body, title="pipeline start", border_style="cyan"))

    def stage_start(self, stage: StageName, note: str = "") -> None:
        msg = f"[bold yellow]► {stage.value}[/]"
        if note:
            msg += f"  [dim]{note}[/]"
        self.console.print(msg)

    def stage_end(self, stage: StageName, status: StageStatus, note: str = "") -> None:
        colour = {
            StageStatus.PASSED: "green",
            StageStatus.FAILED: "red",
            StageStatus.REJECTED: "red",
            StageStatus.SKIPPED: "dim",
        }.get(status, "white")
        symbol = "✔" if status == StageStatus.PASSED else "✘" if status in (StageStatus.FAILED, StageStatus.REJECTED) else "·"
        msg = f"[{colour}]{symbol} {stage.value} — {status.value}[/]"
        if note:
            msg += f"  [dim]{note}[/]"
        self.console.print(msg)

    def info(self, message: str) -> None:
        self.console.print(f"  {message}")

    def warn(self, message: str) -> None:
        self.console.print(f"  [yellow]! {message}[/]")

    def error(self, message: str) -> None:
        self.console.print(f"  [red]✘ {message}[/]")

    # ------------------------------------------------------------------
    # Verbose helpers
    # ------------------------------------------------------------------

    def prompt(self, stage: StageName, version: str, text: str) -> None:
        if not self.verbose:
            return
        self.console.print(
            Panel(
                Syntax(text, "markdown", word_wrap=True, theme="ansi_dark"),
                title=f"prompt → {stage.value} ({version})",
                border_style="blue",
            )
        )

    def response(self, stage: StageName, text: str) -> None:
        if not self.verbose:
            return
        self.console.print(
            Panel(
                Syntax(text, "json", word_wrap=True, theme="ansi_dark"),
                title=f"response ← {stage.value}",
                border_style="magenta",
            )
        )

    def gate_output(self, gate: str, stdout: str, stderr: str, passed: bool) -> None:
        if passed and not self.verbose:
            return
        tail = (stdout or stderr or "").strip().splitlines()[-15:]
        body = "\n".join(tail) if tail else "(no output)"
        self.console.print(
            Panel(
                body,
                title=f"gate: {gate} ({'PASS' if passed else 'FAIL'})",
                border_style="green" if passed else "red",
            )
        )

    # ------------------------------------------------------------------
    # Final report
    # ------------------------------------------------------------------

    def final_report(self, run: PipelineRun, m: RunMetrics) -> None:
        self.console.rule("[bold cyan]Pipeline complete")

        table = Table(title="Stage timings", show_header=True, header_style="bold")
        table.add_column("Stage")
        table.add_column("Status")
        table.add_column("Duration", justify="right")
        table.add_column("Tokens in", justify="right")
        table.add_column("Tokens out", justify="right")
        for stage in _STAGE_ORDER:
            result = run.stage_results.get(stage)
            if result is None:
                continue
            status_style = {
                StageStatus.PASSED: "green",
                StageStatus.FAILED: "red",
                StageStatus.REJECTED: "red",
                StageStatus.SKIPPED: "dim",
            }.get(result.status, "white")
            table.add_row(
                stage.value,
                f"[{status_style}]{result.status.value}[/]",
                f"{result.duration_ms} ms",
                f"{result.tokens_in:,}",
                f"{result.tokens_out:,}",
            )
        self.console.print(table)

        totals = Table(title="Totals", show_header=False)
        totals.add_column("metric")
        totals.add_column("value", justify="right")
        totals.add_row("total duration", f"{m.total_duration_ms} ms")
        totals.add_row("total tokens in / out", f"{m.total_tokens_in:,} / {m.total_tokens_out:,}")
        totals.add_row("estimated cost", f"${m.total_cost_usd:.4f}")
        totals.add_row("files generated", f"{m.files_generated} ({m.lines_generated} lines)")
        totals.add_row("tests generated", f"{m.tests_generated} ({m.test_lines_generated} lines)")
        totals.add_row(
            "AC coverage", f"{m.ac_covered}/{m.ac_total} ({m.ac_coverage_pct:.1f}%)"
        )
        totals.add_row(
            "gate pass rate",
            f"{m.gates_passed}/{m.gates_run} ({m.gate_pass_rate:.1f}%)",
        )
        self.console.print(totals)

        if run.audit_dir is not None:
            self.console.print(
                Panel(
                    _render_tree(run.audit_dir),
                    title=f"audit directory: {run.audit_dir}",
                    border_style="cyan",
                )
            )

        if m.halted:
            self.console.print(f"[bold red]HALTED:[/] {m.halt_reason}")
        else:
            self.console.print("[bold green]All stages passed.[/]")

    def gate_summary(self, gates: Iterable[GateResult]) -> None:
        table = Table(title="Quality gates", show_header=True, header_style="bold")
        table.add_column("Gate")
        table.add_column("Status")
        table.add_column("Duration", justify="right")
        for g in gates:
            colour = "green" if g.passed else "red"
            table.add_row(
                g.gate.value,
                f"[{colour}]{'PASS' if g.passed else 'FAIL'}[/]",
                f"{g.duration_ms} ms",
            )
        self.console.print(table)


def _render_tree(root: Path) -> str:
    lines: list[str] = [root.name + "/"]

    def walk(p: Path, prefix: str = "") -> None:
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
        for idx, entry in enumerate(entries):
            connector = "└── " if idx == len(entries) - 1 else "├── "
            lines.append(prefix + connector + entry.name + ("/" if entry.is_dir() else ""))
            if entry.is_dir():
                extension = "    " if idx == len(entries) - 1 else "│   "
                walk(entry, prefix + extension)

    walk(root)
    return "\n".join(lines)
