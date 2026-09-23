"""The six kinds of work the platform accepts.

Only the opening tasks are declared here. Everything after planning is injected
into the graph at runtime, and the shape differs per kind: a bug fix reproduces
before it repairs, a refactor writes no tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..models import RequirementKind, RiskLevel, Task

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    requirement: str
    kind: RequirementKind
    tasks: list[Task] = field(default_factory=list)
    seed_from: Path | None = None
    description: str = ""
    # The kind of engineering work, which is finer-grained than greenfield vs
    # brownfield: enhancing, fixing, restructuring and documenting a system are
    # different jobs with different evidence of success.
    work: str = "build"
    # Checks that only mean something for this kind of change. Preserving
    # behaviour matters for a refactor and nowhere else; coverage matters when
    # the deliverable is tests.
    checks: tuple[str, ...] = ()
    # Whether the requirement as stated leaves a blocking question open.
    well_defined: bool = False

    def graph_tasks(self) -> list[Task]:
        """Fresh Task instances per run; tasks carry mutable execution state."""
        return [t.model_copy(deep=True) for t in self.tasks]


GREENFIELD = Scenario(
    name="url_shortener",
    title="Greenfield — build a URL shortener",
    requirement=(
        "Build a scalable URL shortener service with APIs, persistence, and analytics."
    ),
    kind=RequirementKind.GREENFIELD,
    work="build",
    description=(
        "Nothing exists. The run has to recover the engineering problem from one "
        "sentence, decide what it is not being told, design a system, build it, and "
        "prove it works."
    ),
    tasks=[
        Task(
            id="T-001",
            title="Normalize the requirement and surface what it does not say",
            agent="requirement",
            reads=[],
            writes=["requirement"],
            rationale="Everything downstream traces to the ids this task assigns",
        ),
        Task(
            id="T-002",
            title="Design the system and record what each decision costs",
            agent="architecture",
            depends_on=["T-001"],
            reads=["requirement"],
            writes=["architecture"],
            rationale="Independent of the API contract, so the two are designed concurrently",
        ),
        Task(
            id="T-003",
            title="Define the HTTP contract",
            agent="api_design",
            depends_on=["T-001"],
            reads=["requirement"],
            writes=["api_contract"],
            rationale=(
                "Derived from the requirements, not from the architecture; validation "
                "later holds the implementation to it"
            ),
        ),
        Task(
            id="T-004",
            title="Plan the build and inject it into the running graph",
            agent="planner",
            depends_on=["T-002", "T-003"],
            reads=["requirement", "architecture", "api_contract"],
            writes=["work_plan"],
            rationale="Needs both the design and the contract before it can decompose work",
        ),
    ],
)


BROWNFIELD = Scenario(
    name="analytics_upgrade",
    title="Brownfield — add click analytics to an existing service",
    requirement=(
        "Add click analytics to the existing URL shortener without slowing down "
        "redirects or breaking the current test suite."
    ),
    kind=RequirementKind.BROWNFIELD,
    seed_from=REPO_ROOT / "sample_codebase" / "url_shortener_legacy",
    work="enhancement",
    description=(
        "A working service already exists and its suite passes. The run has to find "
        "out what the change touches before proposing one, and the existing tests are "
        "the constraint it is measured against."
    ),
    tasks=[
        Task(
            id="T-001",
            title="Normalize the requirement and surface what it does not say",
            agent="requirement",
            reads=[],
            writes=["requirement"],
        ),
        Task(
            id="T-002",
            title="Analyze the existing codebase and compute the blast radius",
            agent="codebase",
            depends_on=["T-001"],
            reads=["requirement"],
            writes=["impact_analysis", "codebase_summary"],
            rationale=(
                "Static analysis, not recall: which files import which is a fact, and "
                "designing against a guess sends the build after the wrong modules"
            ),
        ),
        Task(
            id="T-003",
            title="Design the change against the existing structure",
            agent="architecture",
            depends_on=["T-002"],
            reads=["requirement", "impact_analysis"],
            writes=["architecture"],
        ),
        Task(
            id="T-004",
            title="Define the contract for the new surface",
            agent="api_design",
            depends_on=["T-002"],
            reads=["requirement", "impact_analysis"],
            writes=["api_contract"],
            rationale="Independent of the architecture task, so they run concurrently",
        ),
        Task(
            id="T-005",
            title="Plan the change and inject it into the running graph",
            agent="planner",
            depends_on=["T-003", "T-004"],
            reads=["requirement", "architecture", "api_contract"],
            writes=["work_plan"],
            risk=RiskLevel.LOW,
        ),
    ],
)


BUG_FIX = Scenario(
    name="fix_expiry_bug",
    title="Brownfield — fix expired links that keep redirecting",
    requirement=(
        "Expired short links keep redirecting instead of returning 404. Find the "
        "cause, fix it, and prove the fix without making redirects slower."
    ),
    kind=RequirementKind.BROWNFIELD,
    seed_from=REPO_ROOT / "sample_codebase" / "url_shortener_legacy",
    work="bug_fix",
    well_defined=True,
    description=(
        "A real latent defect in the target codebase, which its own 32 passing tests "
        "do not catch. The run must reproduce it before repairing it: the regression "
        "test is run against the unfixed code first, and the run stops if it passes."
    ),
    tasks=[
        Task(
            id="T-001",
            title="Normalize the defect report",
            agent="requirement",
            reads=[],
            writes=["requirement"],
            rationale=(
                "A reported defect with an observable symptom is well defined; there "
                "is nothing blocking to ask about"
            ),
        ),
        Task(
            id="T-002",
            title="Locate the defect in the existing code",
            agent="codebase",
            depends_on=["T-001"],
            reads=["requirement"],
            writes=["impact_analysis", "codebase_summary"],
            rationale="Which module owns the read path is a fact, not a recollection",
        ),
        Task(
            id="T-003",
            title="Restate the contract the defect violates",
            agent="api_design",
            depends_on=["T-002"],
            reads=["requirement", "impact_analysis"],
            writes=["api_contract"],
            rationale=(
                "A bug fix designs nothing new; it restates the promise the code is "
                "breaking so validation can hold the fix to it"
            ),
        ),
        Task(
            id="T-004",
            title="Plan reproduce-then-repair and inject it",
            agent="planner",
            depends_on=["T-003"],
            reads=["requirement", "impact_analysis", "api_contract"],
            writes=["work_plan"],
            rationale="No architecture stage: nothing is being designed",
        ),
    ],
)


REFACTOR = Scenario(
    name="extract_service_layer",
    title="Brownfield — extract a service layer, change no behaviour",
    requirement=(
        "The route handlers contain business logic. Extract a service layer so the "
        "rules can be tested without HTTP, without changing what any endpoint does."
    ),
    kind=RequirementKind.BROWNFIELD,
    seed_from=REPO_ROOT / "sample_codebase" / "url_shortener_legacy",
    work="refactor",
    checks=("behaviour_preserved",),
    description=(
        "The one kind of change whose success criterion is that nothing observable "
        "happened. The existing suite is the specification and must pass unedited, so "
        "the run fails if it touches a single pre-existing test file."
    ),
    tasks=[
        Task(
            id="T-001",
            title="Normalize the refactoring goal and its constraints",
            agent="requirement",
            reads=[],
            writes=["requirement"],
        ),
        Task(
            id="T-002",
            title="Map the current structure and what depends on it",
            agent="codebase",
            depends_on=["T-001"],
            reads=["requirement"],
            writes=["impact_analysis", "codebase_summary"],
            rationale="A restructuring that does not know its callers breaks them",
        ),
        Task(
            id="T-003",
            title="Design the target structure and the seam it introduces",
            agent="architecture",
            depends_on=["T-002"],
            reads=["requirement", "impact_analysis"],
            writes=["architecture"],
        ),
        Task(
            id="T-004",
            title="Restate the existing surface so it can be proved unchanged",
            agent="api_design",
            depends_on=["T-002"],
            reads=["requirement", "impact_analysis"],
            writes=["api_contract"],
            rationale=(
                "Independent of the architecture task, and it is what catches an "
                "endpoint silently dropped during the restructuring"
            ),
        ),
        Task(
            id="T-005",
            title="Plan the extraction and inject it",
            agent="planner",
            depends_on=["T-003", "T-004"],
            reads=["requirement", "architecture", "api_contract"],
            writes=["work_plan"],
        ),
    ],
)


TEST_IMPROVEMENT = Scenario(
    name="raise_test_coverage",
    title="Test improvement — put the untested modules under test",
    requirement=(
        "The cache and the repository have no tests of their own. Add direct tests "
        "for their behaviour, including the edge cases, without changing any code."
    ),
    kind=RequirementKind.BROWNFIELD,
    seed_from=REPO_ROOT / "sample_codebase" / "url_shortener_legacy",
    work="test_improvement",
    checks=("test_coverage",),
    well_defined=True,
    description=(
        "The deliverable is tests, not code. There is no architecture or contract "
        "stage because nothing is being designed, and the run reports which modules "
        "are still unreferenced by any test rather than claiming an improvement."
    ),
    tasks=[
        Task(
            id="T-001",
            title="Normalize the testing goal",
            agent="requirement",
            reads=[],
            writes=["requirement"],
        ),
        Task(
            id="T-002",
            title="Find which modules nothing tests",
            agent="codebase",
            depends_on=["T-001"],
            reads=["requirement"],
            writes=["impact_analysis", "codebase_summary"],
            rationale="The gap is read off the codebase rather than guessed at",
        ),
        Task(
            id="T-003",
            title="Plan the test work and inject it",
            agent="planner",
            depends_on=["T-002"],
            reads=["requirement", "impact_analysis"],
            writes=["work_plan"],
            rationale="Neither an architecture nor a contract is needed to write tests",
        ),
    ],
)


DOCUMENTATION = Scenario(
    name="document_the_service",
    title="Documentation improvement — write the reference, ADRs and runbook",
    requirement=(
        "The service has no API reference, no record of why it was built this way, "
        "and nothing an on-call engineer can use. Document it."
    ),
    kind=RequirementKind.BROWNFIELD,
    seed_from=REPO_ROOT / "sample_codebase" / "url_shortener_legacy",
    work="documentation",
    checks=("documentation",),
    well_defined=True,
    description=(
        "The deliverable is documents. Success is checked against the API contract "
        "rather than a word count, so a run cannot pass by writing three more pages "
        "about the parts that were already understood."
    ),
    tasks=[
        Task(
            id="T-001",
            title="Normalize what has to be documented, and for whom",
            agent="requirement",
            reads=[],
            writes=["requirement"],
        ),
        Task(
            id="T-002",
            title="Read the codebase that is being documented",
            agent="codebase",
            depends_on=["T-001"],
            reads=["requirement"],
            writes=["impact_analysis", "codebase_summary"],
            rationale="Documentation written without reading the code documents a guess",
        ),
        Task(
            id="T-003",
            title="Establish the published surface that must be covered",
            agent="api_design",
            depends_on=["T-002"],
            reads=["requirement", "impact_analysis"],
            writes=["api_contract"],
            rationale="Gives the documentation check something objective to measure against",
        ),
        Task(
            id="T-004",
            title="Plan the documentation work and inject it",
            agent="planner",
            depends_on=["T-003"],
            reads=["requirement", "impact_analysis", "api_contract"],
            writes=["work_plan"],
        ),
    ],
)


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (GREENFIELD, BROWNFIELD, BUG_FIX, REFACTOR, TEST_IMPROVEMENT, DOCUMENTATION)
}


def get(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise KeyError(
            f"unknown scenario {name!r}; available: {', '.join(sorted(SCENARIOS))}"
        ) from None


__all__ = [
    "BROWNFIELD",
    "BUG_FIX",
    "DOCUMENTATION",
    "GREENFIELD",
    "REFACTOR",
    "SCENARIOS",
    "TEST_IMPROVEMENT",
    "Scenario",
    "get",
]
