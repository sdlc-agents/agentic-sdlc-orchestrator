"""Prove the bug is real before anyone is allowed to fix it.

Runs the new regression test and requires it to fail. Without that, a bug-fix
run can produce a test that was green all along and a fix that changed nothing.
"""

from __future__ import annotations

from pathlib import Path

from ..models import ArtifactKind
from ..orchestration.engine import Agent, AgentContext, AgentResult
from ..orchestration.errors import FatalError
from ..tools import failing_tests, run_pytest
from ..tools.workspace import Workspace
from .base import ProviderAgent


class ReproductionAgent(Agent):
    name = "reproduction"

    def __init__(self, test_timeout_s: int = 180):
        self.test_timeout_s = test_timeout_s

    def run(self, ctx: AgentContext) -> AgentResult:
        workspace = Workspace(Path(ctx.workspace))
        index = ctx.read("test_suite_index")
        targets = sorted(index)

        run = run_pytest(workspace.root, timeout_s=self.test_timeout_s)
        if not run.executed:
            raise FatalError(
                f"the regression suite could not be executed: {run.reason}. "
                "Without a run there is no evidence either way."
            )

        failed = [name.replace("\\", "/") for name in failing_tests(run.output)]
        reproduced = [
            name for name in failed if any(name.startswith(t) for t in targets)
        ]

        if not reproduced:
            # Escalate rather than continue. Either the defect does not exist as
            # described, or the test does not exercise it; both need a person,
            # and neither is fixed by writing a patch.
            raise FatalError(
                f"the regression test passed against the unfixed code "
                f"({run.summary()}). The defect was not reproduced, so there is "
                "nothing to verify a fix against."
            )

        report = ProviderAgent.artifact(
            path="reproduction.md",
            content=_render(targets, reproduced, run.summary(), run.output),
            produced_by=ctx.task.id,
            kind=ArtifactKind.REPORT,
            language="markdown",
        )

        return AgentResult(
            writes={
                "reproduction": {
                    "reproduced": reproduced,
                    "summary": run.summary(),
                    "baseline_failures": failed,
                }
            },
            artifacts=[report],
            note=(
                f"defect reproduced: {len(reproduced)} regression test(s) fail against "
                f"the unfixed code ({run.summary()})"
            ),
        )


def _render(targets: list[str], reproduced: list[str], summary: str, output: str) -> str:
    lines = [
        "# Reproduction",
        "",
        "The regression test was run **before** any fix was applied, and it failed.",
        "That failure is the evidence the defect exists; without it, a passing suite",
        "after the change would prove nothing.",
        "",
        f"Result: {summary}",
        "",
        "## Tests that reproduce the defect",
        "",
    ]
    lines += [f"- `{name}`" for name in reproduced]
    lines += ["", "## Files added for this reproduction", ""]
    lines += [f"- `{path}`" for path in targets]
    lines += ["", "## Captured output", "", "```", output[-2000:].strip(), "```", ""]
    return "\n".join(lines)


__all__ = ["ReproductionAgent"]
