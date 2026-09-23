"""Schemas the provider must fill.

Every model call requests one of these, so an agent receives a validated object
rather than prose and a malformed response is a retryable `ContractError`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..models import ArtifactKind, RiskLevel


class TaskSpec(BaseModel):
    """A task the planner wants added to the running graph."""

    id: str = Field(pattern=r"^T-\d{3}$")
    title: str
    agent: str
    depends_on: list[str] = []
    covers: list[str] = []
    reads: list[str] = []
    writes: list[str] = []
    risk: RiskLevel = RiskLevel.LOW
    rationale: str = ""


class WorkPlan(BaseModel):
    """The planner's decomposition, expressed as graph nodes rather than prose."""

    strategy: str
    tasks: list[TaskSpec] = []
    parallelism_note: str = ""

    @property
    def task_ids(self) -> list[str]:
        return [t.id for t in self.tasks]


class GeneratedFile(BaseModel):
    """One file to write, or one surgical edit to an existing file.

    With `replaces` set, `content` replaces that anchor in the file already on
    disk; a missing anchor is an error rather than a silent overwrite. Rewriting
    a module to change six lines produces a diff nobody can review.
    """

    path: str
    content: str
    kind: ArtifactKind = ArtifactKind.CODE
    language: str | None = "python"
    covers: list[str] = []
    replaces: str | None = None


class CodeBundle(BaseModel):
    """A set of files plus the reasoning that produced them."""

    summary: str
    files: list[GeneratedFile] = []
    notes: list[str] = []

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]


class RepairEdit(BaseModel):
    """One machine-applicable change derived from one validation finding.

    `addresses` ties the edit back to the finding that justified it, so a repair
    round can be audited: every edit names the defect it closes.
    """

    path: str
    action: Literal["create", "append", "replace"]
    content: str
    addresses: str
    anchor: str | None = None


class RepairPlan(BaseModel):
    summary: str
    edits: list[RepairEdit] = []
    unrepairable: list[str] = []


class RunSummary(BaseModel):
    headline: str
    what_was_built: list[str] = []
    key_decisions: list[str] = []
    open_risks: list[str] = []
    next_steps: list[str] = []
    # What the run does not establish. Separate from `next_steps` on purpose:
    # a limitation is a fact about this result, not a task someone will pick up.
    limitations: list[str] = []
