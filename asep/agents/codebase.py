"""Brownfield analysis, answered from the AST rather than from recall.

Reports what a change touches, and the structure it is touching: layers, entry
points and data flows recovered from the import graph.
"""

from __future__ import annotations

from pathlib import Path

from ..models import (
    ArtifactKind,
    DataFlow,
    ImpactAnalysis,
    ImpactedElement,
    NormalizedRequirement,
)
from ..orchestration.engine import Agent, AgentContext, AgentResult
from ..tools import CodebaseIndex, scan
from ..tools.impact import analyse, missing_concepts, salient_terms
from .base import ProviderAgent

# Module name -> layer, first match wins. Tests are checked first so
# `test_api.py` is not claimed by the `api` rule.
LAYER_RULES: list[tuple[str, str, str]] = [
    ("tests", "test", "The suite that constrains changes"),
    ("transport", "routes", "Accepts requests and serializes responses"),
    ("transport", "api", "Accepts requests and serializes responses"),
    ("composition", "main", "Constructs every dependency and owns their lifecycle"),
    ("configuration", "config", "Runtime settings, read once at startup"),
    ("domain", "service", "Business rules, independent of transport and storage"),
    ("persistence", "repository", "Reads and writes durable state"),
    ("storage", "db", "The database connection and schema"),
    ("cache", "cache", "In-process read cache in front of storage"),
    ("contracts", "schemas", "Request and response models"),
    ("contracts", "models", "Domain entities"),
    ("async work", "analytics", "Work performed off the request path"),
]

MAX_NEW_CONCEPTS = 3


def classify(module_path: str) -> str:
    """Place a module in the stack by name.

    A naming heuristic, and reported as one: the layer is printed beside the
    file so a mislabel is visible rather than hidden.
    """
    stem = Path(module_path).stem
    parts = set(Path(module_path).parts)
    for layer, token, _ in LAYER_RULES:
        if token == stem or token in parts or token in stem:
            return layer
    return "application"


layer_of = classify


def describe_layer(layer: str) -> str:
    for name, _, description in LAYER_RULES:
        if name == layer:
            return description
    return "Application code"


def reconstruct_layers(index: CodebaseIndex) -> dict[str, list[str]]:
    layers: dict[str, list[str]] = {}
    for module in index.modules:
        if module.path.endswith(".py"):
            layers.setdefault(classify(module.path), []).append(module.path)
    return {layer: sorted(paths) for layer, paths in sorted(layers.items())}


def derive_data_flows(index: CodebaseIndex) -> list[DataFlow]:
    """Recover where data moves between layers, from the import graph.

    An import from A to B means A can call into B. Weaker than a call graph —
    it shows what can happen rather than what does — but every edge it reports
    is genuinely in the source.
    """
    flows: list[DataFlow] = []
    seen: set[tuple[str, str]] = set()

    for module in index.modules:
        if not module.path.endswith(".py") or module.path.endswith("__init__.py"):
            continue
        source_layer = classify(module.path)
        if source_layer == "tests":
            continue

        for other in index.modules:
            if other.path == module.path or not other.path.endswith(".py"):
                continue
            if module.path not in index.importers_of(other.path):
                continue
            target_layer = classify(other.path)
            if source_layer == target_layer:
                continue
            edge = (module.path, other.path)
            if edge in seen:
                continue
            seen.add(edge)
            flows.append(
                DataFlow(
                    source=module.path,
                    target=other.path,
                    description=f"{source_layer} -> {target_layer}",
                )
            )
    return sorted(flows, key=lambda f: (f.source, f.target))


class CodebaseAgent(Agent):
    name = "codebase"

    def __init__(self, root: Path | None = None):
        # Defaults to the run workspace, which for a brownfield run is a copy of
        # the target repository, so analysis stays inside the same boundary as
        # every write.
        self.root = Path(root) if root else None

    def run(self, ctx: AgentContext) -> AgentResult:
        root = self.root or Path(ctx.workspace)
        index = scan(root, include_tests=True)
        requirement: NormalizedRequirement = ctx.read("requirement")

        terms = salient_terms(
            requirement.intent,
            *(f.statement for f in requirement.functional),
            *(n.statement for n in requirement.non_functional),
        )

        def read(relative: str) -> str:
            return (root / relative).read_text(encoding="utf-8", errors="replace")

        direct, reached = analyse(index, terms, read)
        concepts = missing_concepts(index, terms)[:MAX_NEW_CONCEPTS]

        impacted: list[ImpactedElement] = []
        for match in direct + reached:
            impacted.append(
                ImpactedElement(
                    path=match.path,
                    element=Path(match.path).stem,
                    kind="route" if index.by_path[match.path].routes else "module",
                    change="modify",
                    reason="; ".join(match.reasons[:3]),
                    referenced_by=index.importers_of(match.path),
                )
            )
        for concept in concepts:
            impacted.append(
                ImpactedElement(
                    path=f"app/{concept}.py",
                    element=concept,
                    kind="module",
                    change="add",
                    reason=(
                        f"the requirement relies on '{concept}', which no module, "
                        "symbol or table in the codebase provides"
                    ),
                )
            )

        blast = sorted({p for e in impacted for p in ([e.path] + e.referenced_by)})
        regression = sorted(
            m.path for m in index.modules if Path(m.path).name.startswith("test_")
        )
        touched = {e.path for e in impacted}
        impacted_apis = [
            f"{route.method} {route.path}"
            for path, route in index.routes
            if path in touched
        ]

        analysis = ImpactAnalysis(
            root=str(root),
            modules_scanned=len(index.modules),
            impacted=impacted,
            blast_radius=blast,
            regression_surface=regression,
            layers=reconstruct_layers(index),
            data_flows=derive_data_flows(index),
            entry_points=sorted({path for path, _ in index.routes}),
            impacted_apis=impacted_apis,
            notes=_notes(index, regression, impacted_apis, concepts, len(direct), len(reached)),
        )

        report = ProviderAgent.artifact(
            path="docs/impact-analysis.md",
            content=_render(analysis, index.summary(), requirement, terms),
            produced_by=ctx.task.id,
            kind=ArtifactKind.DOC,
            language="markdown",
        )
        ProviderAgent.workspace(ctx).write(report.path, report.content)

        return AgentResult(
            writes={"impact_analysis": analysis, "codebase_summary": index.summary()},
            artifacts=[report],
            note=(
                f"scanned {analysis.modules_scanned} module(s) across "
                f"{len(analysis.layers)} layer(s); {len(direct)} matched the "
                f"requirement, {len(reached)} reached through imports, "
                f"{len(concepts)} concept(s) have no home yet; "
                f"{len(impacted_apis)} API(s) affected"
            ),
        )


def _notes(
    index: CodebaseIndex,
    regression: list[str],
    impacted_apis: list[str],
    concepts: list[str],
    direct: int,
    reached: int,
) -> list[str]:
    notes = [
        f"{len(index.routes)} existing route(s): "
        + ", ".join(f"{r.method} {r.path}" for _, r in index.routes),
        f"existing tables: {', '.join(index.tables) or 'none'}",
        (
            f"{direct} file(s) matched the requirement's own vocabulary; {reached} "
            "more were reached through the import graph and are at risk without "
            "being named"
        ),
    ]
    if concepts:
        notes.append(
            "no module provides " + ", ".join(f"'{c}'" for c in concepts)
            + "; these are the parts that have to be built rather than edited"
        )
    if impacted_apis:
        notes.append(
            "the change reaches " + ", ".join(impacted_apis)
            + ", which callers already depend on"
        )
    notes.append(
        f"{len(regression)} existing test file(s) must keep passing; an additive "
        "change should not need to edit any of them"
    )
    return notes


def _render(analysis: ImpactAnalysis, summary: dict, requirement, terms) -> str:
    lines = [
        "# Impact analysis",
        "",
        f"Target: `{analysis.root}`",
        "",
        f"Scanned {summary['modules']} module(s), {summary['loc']} lines, "
        f"{summary['routes']} route(s).",
        "",
        "## Change being assessed",
        "",
        requirement.intent,
        "",
        "Terms the requirement leans on: "
        + ", ".join(f"`{t}`" for t, _ in terms.most_common(8))
        + ".",
        "",
        "## The system as it stands",
        "",
        "Recovered from the import graph. Layer names come from a naming "
        "convention, so a module whose name says nothing about its role may be "
        "placed loosely — the file is shown beside it.",
        "",
        "| Layer | Responsibility | Modules |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {layer} | {describe_layer(layer)} | "
        + ", ".join(f"`{p}`" for p in paths)
        + " |"
        for layer, paths in analysis.layers.items()
    ]

    if analysis.entry_points:
        lines += [
            "",
            "Entry points: " + ", ".join(f"`{p}`" for p in analysis.entry_points) + ".",
        ]

    if analysis.data_flows:
        touched = set(analysis.impacted_paths)
        lines += [
            "",
            "## Data flows",
            "",
            "A flow marked **changed** passes through a file this change "
            "modifies, which is where a regression would come from.",
            "",
            "| From | To | Direction | Touched |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{f.source}` | `{f.target}` | {f.description} | "
            f"{'**changed**' if f.source in touched or f.target in touched else 'no'} |"
            for f in analysis.data_flows
        ]

    if analysis.impacted_apis:
        lines += ["", "## APIs affected", ""]
        lines += [f"- `{api}`" for api in analysis.impacted_apis]

    lines += [
        "",
        "## Impacted elements",
        "",
        "Derived: each row says why the file is here, not merely that it is.",
        "",
        "| File | Layer | Change | Why | Imported by |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| `{e.path}` | {layer_of(e.path)} | {e.change} | {e.reason} | "
        f"{', '.join(f'`{p}`' for p in e.referenced_by) or '—'} |"
        for e in analysis.impacted
    ]

    lines += ["", "## Blast radius", ""]
    lines += [f"- `{path}`" for path in analysis.blast_radius]
    lines += ["", "## Regression surface", ""]
    lines += [f"- `{path}`" for path in analysis.regression_surface] or ["- none"]
    lines += ["", "## Notes", ""]
    lines += [f"- {note}" for note in analysis.notes]
    lines += [""]
    return "\n".join(lines)


__all__ = ["CodebaseAgent", "classify", "derive_data_flows", "reconstruct_layers"]
