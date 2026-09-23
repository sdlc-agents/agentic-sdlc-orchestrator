from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .task import RiskLevel


class Component(BaseModel):
    name: str
    responsibility: str
    technology: str
    scales: Literal["stateless", "stateful", "managed"] = "stateless"


class DataFlow(BaseModel):
    source: str
    target: str
    description: str
    synchronous: bool = True


class TradeOff(BaseModel):
    decision: str
    chosen: str
    alternatives: list[str]
    rationale: str
    accepted_cost: str


class Risk(BaseModel):
    id: str
    description: str
    likelihood: Literal["low", "medium", "high"]
    impact: Literal["low", "medium", "high"]
    mitigation: str

    @property
    def severity(self) -> RiskLevel:
        rank = {"low": 1, "medium": 2, "high": 3}
        score = rank[self.likelihood] * rank[self.impact]
        if score >= 6:
            return RiskLevel.HIGH
        if score >= 3:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW


class Architecture(BaseModel):
    style: str
    summary: str
    components: list[Component] = []
    data_flows: list[DataFlow] = []
    trade_offs: list[TradeOff] = []
    risks: list[Risk] = []
    diagram: str = ""


class Endpoint(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    summary: str
    covers: list[str] = []
    request_model: str | None = None
    response_model: str | None = None
    status_codes: list[int] = [200]


class ApiContract(BaseModel):
    title: str
    version: str = "1.0.0"
    endpoints: list[Endpoint] = []

    def openapi(self) -> dict:
        paths: dict[str, dict] = {}
        for ep in self.endpoints:
            responses = {
                str(code): {"description": "response"} for code in ep.status_codes
            }
            paths.setdefault(ep.path, {})[ep.method.lower()] = {
                "summary": ep.summary,
                "responses": responses,
            }
        return {
            "openapi": "3.0.3",
            "info": {"title": self.title, "version": self.version},
            "paths": paths,
        }


class ImpactedElement(BaseModel):
    path: str
    element: str
    kind: Literal["module", "class", "function", "route", "model", "test", "migration"]
    change: Literal["modify", "add", "remove"]
    reason: str
    referenced_by: list[str] = []


class ImpactAnalysis(BaseModel):
    """Output of static analysis over an existing codebase, not a guess.

    `layers`, `data_flows` and `entry_points` reconstruct the system from the
    import graph: whether a change is risky depends on what flows through the
    file, which is a property of the architecture rather than the file.
    """

    root: str
    modules_scanned: int = 0
    impacted: list[ImpactedElement] = []
    blast_radius: list[str] = []
    regression_surface: list[str] = []
    notes: list[str] = []

    # The existing architecture, recovered rather than assumed.
    layers: dict[str, list[str]] = {}
    data_flows: list[DataFlow] = []
    entry_points: list[str] = []
    impacted_apis: list[str] = []

    @property
    def impacted_paths(self) -> list[str]:
        return sorted({e.path for e in self.impacted})

    @property
    def flows_through_impacted_code(self) -> list[DataFlow]:
        """The flows that pass through something this change touches."""
        touched = set(self.impacted_paths)
        return [f for f in self.data_flows if f.source in touched or f.target in touched]
