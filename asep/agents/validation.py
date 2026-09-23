"""Independent verification.

No model is consulted: this parses the files on disk, compares routes against
the approved contract, and executes the generated suite in a subprocess.
"""

from __future__ import annotations

from pathlib import Path

from ..models import ArtifactKind, CheckStatus, ValidationReport
from ..orchestration.engine import Agent, AgentContext, AgentResult
from ..tools.workspace import Workspace
from ..validation import CheckContext, render_approach, resolve_checks, run_checks
from .base import ProviderAgent


class ValidationAgent(Agent):
    name = "validation"

    def __init__(
        self,
        run_tests: bool = True,
        test_timeout_s: int = 180,
        extra_checks: tuple[str, ...] = (),
    ):
        self.run_tests = run_tests
        self.test_timeout_s = test_timeout_s
        # Scenarios opt into checks that only mean something for their kind of
        # change; the default battery applies to every run.
        self.checks = resolve_checks(extra_checks)

    def run(self, ctx: AgentContext) -> AgentResult:
        attempt = int(ctx.get("validation_rounds", 0)) + 1

        check_ctx = CheckContext(
            workspace=Workspace(Path(ctx.workspace)),
            artifacts=ctx.state.artifacts,
            requirement=ctx.get("requirement"),
            api_contract=ctx.get("api_contract"),
            architecture=ctx.get("architecture"),
            attempt=attempt,
            run_tests=self.run_tests,
            test_timeout_s=self.test_timeout_s,
            baseline_digests=ctx.get("baseline_digests") or {},
        )
        report = ValidationReport.from_checks(
            run_checks(check_ctx, self.checks), attempt=attempt
        )

        # A report belongs to the run record rather than the deliverable, so it is
        # published as an artifact and written to the run directory, not into the
        # workspace the next round would re-scan.
        rendered = ProviderAgent.artifact(
            path=f"validation-round-{attempt}.md",
            content=_render(report),
            produced_by=ctx.task.id,
            kind=ArtifactKind.REPORT,
            language="markdown",
        )

        artifacts = [rendered]
        if attempt == 1:
            # Written once, before any repair has had a chance to change the
            # picture: this is the approach, not a description of the outcome.
            artifacts.append(
                ProviderAgent.artifact(
                    path="validation-approach.md",
                    content=render_approach(
                        check_ctx.workspace,
                        [c.name for c in report.checks],
                        requirement=check_ctx.requirement,
                        api_contract=check_ctx.api_contract,
                    ),
                    produced_by=ctx.task.id,
                    kind=ArtifactKind.REPORT,
                    language="markdown",
                )
            )

        failed = [c.name for c in report.checks if c.status == CheckStatus.FAIL]
        return AgentResult(
            writes={"validation_report": report, "validation_rounds": attempt},
            artifacts=artifacts,
            note=(
                f"round {attempt}: {report.status.value} — "
                f"{len(report.checks)} check(s), {len(report.errors)} error(s), "
                f"{len(report.warnings)} warning(s)"
                + (f"; failed: {', '.join(failed)}" if failed else "")
            ),
        )


def _render(report: ValidationReport) -> str:
    lines = [
        f"# Validation round {report.attempt}",
        "",
        f"**Result: {report.status.value.upper()}** — {len(report.errors)} error(s), "
        f"{len(report.warnings)} warning(s), {len(report.repairable)} machine-repairable.",
        "",
        "| Check | Status | Detail | Time |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {c.name} | {c.status.value} | {c.detail} | {c.duration_ms}ms |"
        for c in report.checks
    ]

    if report.errors:
        lines += ["", "## Errors", ""]
        for finding in report.errors:
            lines.append(f"- **{finding.check}** — {finding.message}")
            if finding.target_path:
                lines.append(f"  - file: `{finding.target_path}`")
            if finding.repair_hint:
                lines.append(f"  - repair: {finding.repair_hint}")
            else:
                lines.append("  - repair: no machine-actionable hint; needs a human")

    if report.warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"- **{f.check}** — {f.message}" for f in report.warnings]

    lines += [""]
    return "\n".join(lines)


__all__ = ["ValidationAgent"]
