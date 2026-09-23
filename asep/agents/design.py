"""Architecture and API contract.

Siblings rather than a sequence: both depend on the normalized requirement and
neither on the other, so the engine runs them concurrently.
"""

from __future__ import annotations

import json

from ..models import ApiContract, Architecture, ArtifactKind
from ..orchestration.engine import AgentContext, AgentResult
from ..orchestration.errors import ContractError
from .base import ProviderAgent

ARCHITECTURE_INSTRUCTION = """Design the system for these requirements.

State the decisions that are expensive to reverse and say what each one costs.
A trade-off with no accepted cost is not a trade-off, it is a preference. Name
the risks that would actually cause an incident, and give each one a mitigation
that is specific enough to implement. Keep the component list to things that
exist in the deliverable."""

API_INSTRUCTION = """Define the HTTP contract that satisfies these requirements.

Every endpoint must trace to at least one requirement id. Include the status
codes callers must handle, not only the happy path. The contract is what the
tests and the validation stage will hold the implementation to, so do not
declare an endpoint the system is not expected to serve."""


class ArchitectureAgent(ProviderAgent):
    name = "architecture"
    schema = Architecture
    instruction = ARCHITECTURE_INSTRUCTION

    def run(self, ctx: AgentContext) -> AgentResult:
        requirement = ctx.read("requirement")
        architecture: Architecture = self.call(  # type: ignore[assignment]
            ctx,
            requirement=requirement,
            impact_analysis=ctx.get("impact_analysis"),
        )

        if not architecture.trade_offs:
            raise ContractError(
                "architecture declares no trade-offs; a design with no rejected "
                "alternatives has not been designed"
            )

        doc = self.artifact(
            path="docs/architecture.md",
            content=_render_architecture(architecture),
            produced_by=ctx.task.id,
            kind=ArtifactKind.DOC,
            language="markdown",
        )
        self.workspace(ctx).write(doc.path, doc.content)

        high = [r.id for r in architecture.risks if r.severity.value == "high"]
        return AgentResult(
            writes={"architecture": architecture},
            artifacts=[doc],
            note=(
                f"{architecture.style}: {len(architecture.components)} component(s), "
                f"{len(architecture.trade_offs)} trade-off(s), "
                f"{len(architecture.risks)} risk(s)"
                + (f", high severity: {', '.join(high)}" if high else "")
            ),
        )


class ApiDesignAgent(ProviderAgent):
    name = "api_design"
    schema = ApiContract
    instruction = API_INSTRUCTION

    def run(self, ctx: AgentContext) -> AgentResult:
        requirement = ctx.read("requirement")
        contract: ApiContract = self.call(  # type: ignore[assignment]
            ctx,
            requirement=requirement,
            impact_analysis=ctx.get("impact_analysis"),
        )

        if not contract.endpoints:
            raise ContractError("api contract declares no endpoints")

        untraced = [f"{e.method} {e.path}" for e in contract.endpoints if not e.covers]
        if untraced:
            raise ContractError(
                f"endpoints with no requirement traceability: {untraced}"
            )

        # Generated from the model, not written alongside it, so the published
        # spec cannot drift from the contract the validation stage enforces.
        spec = self.artifact(
            path="openapi.yaml",
            content=_to_yaml(contract.openapi()),
            produced_by=ctx.task.id,
            kind=ArtifactKind.API_SPEC,
            language="yaml",
            covers=sorted({rid for e in contract.endpoints for rid in e.covers}),
        )
        self.workspace(ctx).write(spec.path, spec.content)

        return AgentResult(
            writes={"api_contract": contract},
            artifacts=[spec],
            note=(
                f"{len(contract.endpoints)} endpoint(s) v{contract.version}: "
                + ", ".join(f"{e.method} {e.path}" for e in contract.endpoints)
            ),
        )


def _to_yaml(value, indent: int = 0) -> str:
    """Minimal YAML emitter.

    A dependency on PyYAML for one deterministic dump of a dict the platform
    built itself is not worth the install; json is valid YAML for scalars, which
    is what keeps this honest rather than clever.
    """
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return pad + "{}"
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_to_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {json.dumps(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return pad + "[]"
        lines = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                rendered = _to_yaml(item, indent + 1).lstrip()
                lines.append(f"{pad}- {rendered}")
            else:
                lines.append(f"{pad}- {json.dumps(item)}")
        return "\n".join(lines)
    return f"{pad}{json.dumps(value)}"


def _render_architecture(a: Architecture) -> str:
    lines = [
        "# Architecture",
        "",
        f"**Style.** {a.style}",
        "",
        a.summary,
        "",
    ]
    if a.diagram:
        lines += ["## Shape", "", "```mermaid", a.diagram, "```", ""]

    lines += ["## Components", "", "| Component | Responsibility | Technology | Scaling |",
              "| --- | --- | --- | --- |"]
    lines += [
        f"| {c.name} | {c.responsibility} | {c.technology} | {c.scales} |" for c in a.components
    ]

    if a.data_flows:
        lines += ["", "## Data flows", ""]
        lines += [
            f"- {f.source} → {f.target}"
            f" ({'synchronous' if f.synchronous else 'asynchronous'}): {f.description}"
            for f in a.data_flows
        ]

    lines += ["", "## Decisions", ""]
    for t in a.trade_offs:
        lines += [
            f"### {t.decision}",
            "",
            f"**Chosen.** {t.chosen}",
            "",
            f"**Rejected.** {', '.join(t.alternatives)}",
            "",
            f"**Why.** {t.rationale}",
            "",
            f"**Accepted cost.** {t.accepted_cost}",
            "",
        ]

    lines += ["## Risks", "", "| ID | Risk | Likelihood | Impact | Severity | Mitigation |",
              "| --- | --- | --- | --- | --- | --- |"]
    lines += [
        f"| {r.id} | {r.description} | {r.likelihood} | {r.impact} | "
        f"{r.severity.value} | {r.mitigation} |"
        for r in a.risks
    ]
    lines += [""]
    return "\n".join(lines)


__all__ = ["ApiDesignAgent", "ArchitectureAgent"]
