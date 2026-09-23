from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class RequirementKind(str, Enum):
    GREENFIELD = "greenfield"
    BROWNFIELD = "brownfield"


class Clarity(str, Enum):
    CLEAR = "clear"
    NEEDS_CLARIFICATION = "needs_clarification"


class FunctionalRequirement(BaseModel):
    id: str = Field(pattern=r"^FR-\d{3}$")
    statement: str
    priority: Literal["must", "should", "could"] = "must"


class NonFunctionalRequirement(BaseModel):
    id: str = Field(pattern=r"^NFR-\d{3}$")
    category: Literal[
        "performance",
        "scalability",
        "availability",
        "security",
        "observability",
        "cost",
        "maintainability",
    ]
    statement: str
    target: str | None = None


class Ambiguity(BaseModel):
    id: str = Field(pattern=r"^AMB-\d{3}$")
    question: str
    why_it_matters: str
    blocking: bool
    default_assumption: str | None = None


class Assumption(BaseModel):
    id: str = Field(pattern=r"^ASM-\d{3}$")
    statement: str
    source: Literal["stated", "default", "human"]
    resolves: str | None = None


class NormalizedRequirement(BaseModel):
    """The engineering problem, recovered from a sentence of natural language."""

    raw: str
    intent: str
    kind: RequirementKind
    clarity: Clarity
    functional: list[FunctionalRequirement] = []
    non_functional: list[NonFunctionalRequirement] = []
    ambiguities: list[Ambiguity] = []
    assumptions: list[Assumption] = []
    out_of_scope: list[str] = []

    @property
    def blocking_ambiguities(self) -> list[Ambiguity]:
        return [a for a in self.ambiguities if a.blocking]

    @property
    def requirement_ids(self) -> list[str]:
        return [r.id for r in self.functional] + [r.id for r in self.non_functional]
