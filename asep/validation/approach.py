"""The validation approach, derived from the suite and checks that actually ran.

States what each check establishes and, more usefully, what it cannot see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..tools import scan
from ..tools.workspace import Workspace

TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)

# What each check establishes, and — more usefully — what it does not. Keyed by
# check name so the document tracks the battery the run actually used.
CHECK_INTENT: dict[str, tuple[str, str]] = {
    "structure": (
        "The deliverable is a runnable project, not a pile of snippets",
        "Says nothing about whether the code in those files is correct",
    ),
    "syntax": (
        "Every generated module parses",
        "A module can parse perfectly and still be wrong in every other way",
    ),
    "guardrails": (
        "No file on disk contains a forbidden destructive operation",
        "Pattern matching over text; it does not understand intent, and novel "
        "phrasings of a dangerous action are not in the list",
    ),
    "api_contract": (
        "Every endpoint the approved contract declares exists in the code",
        "Checks that a route exists, not that it behaves as the contract describes",
    ),
    "traceability": (
        "Every must-have requirement is claimed by some artifact",
        "An artifact claiming to cover a requirement is not evidence that it does",
    ),
    "tests": (
        "The generated suite executed and passed in a subprocess",
        "Only covers what the suite asserts; a passing suite bounds risk, it does "
        "not eliminate it",
    ),
    "behaviour_preserved": (
        "Pre-existing tests were neither edited nor deleted",
        "Compares file digests, so it cannot detect a test weakened in a way that "
        "leaves the file byte-identical",
    ),
    "test_coverage": (
        "Reports which source modules no test imports at all",
        "Counts imports, not executed lines; a module with one trivial test counts "
        "as covered",
    ),
    "documentation": (
        "Every endpoint in the contract appears in some document",
        "Checks that an endpoint is mentioned, not that what is written is accurate",
    ),
}


@dataclass
class TestFile:
    path: str
    kind: str
    cases: int
    exercises: list[str]


def classify_tests(workspace: Workspace) -> list[TestFile]:
    """Split the suite into unit and integration tests.

    Integration is detected by the test reaching the assembled application — a
    `client` fixture or a TestClient. Everything else imports its subject
    directly and is treated as a unit test.
    """
    index = scan(workspace.root, include_tests=True)
    by_path = {m.path: m for m in index.modules}

    out: list[TestFile] = []
    for path in sorted(workspace.relative_files("**/*.py")):
        if not Path(path).name.startswith("test_"):
            continue
        body = workspace.read(path)
        integration = "TestClient" in body or "(client" in body or "client," in body
        module = by_path.get(path)
        exercises = sorted(
            {
                imported.split(".")[0] if "." not in imported else imported.rsplit(".", 1)[0]
                for imported in (module.imports if module else [])
                if imported.startswith("app")
            }
        )
        out.append(
            TestFile(
                path=path,
                kind="integration" if integration else "unit",
                cases=len(TEST_DEF.findall(body)),
                exercises=exercises,
            )
        )
    return out


def render(
    workspace: Workspace,
    check_names: list[str],
    requirement=None,
    api_contract=None,
) -> str:
    """The approach document: strategy, steps, and what none of it covers."""
    tests = classify_tests(workspace)
    unit = [t for t in tests if t.kind == "unit"]
    integration = [t for t in tests if t.kind == "integration"]
    total_cases = sum(t.cases for t in tests)

    lines = [
        "# Validation approach",
        "",
        "What this run checks, how, and what it cannot tell you. Derived from the "
        "workspace and the configured checks rather than declared in advance, so "
        "it describes the suite that exists rather than the one that was intended.",
        "",
        "## Test strategy",
        "",
    ]

    if tests:
        lines += [
            f"{total_cases} test case(s) across {len(tests)} file(s): "
            f"{len(unit)} unit file(s) and {len(integration)} integration file(s).",
            "",
            "**Unit tests** import their subject directly and assert its behaviour "
            "in isolation — the cheap, fast layer that pins down edge cases which "
            "are awkward to provoke through the front door.",
            "",
            "**Integration tests** drive the assembled application through HTTP. "
            "They are what catches wiring mistakes: a component can be individually "
            "correct and never connected.",
            "",
            "| File | Kind | Cases | Exercises |",
            "| --- | --- | --- | --- |",
        ]
        for t in tests:
            if t.exercises:
                exercises = ", ".join(f"`{m}`" for m in t.exercises)
            elif t.kind == "integration":
                # Reaches the app through a fixture, so it imports nothing
                # directly — which is not the same as exercising nothing.
                exercises = "the assembled application over HTTP"
            else:
                exercises = "—"
            lines.append(f"| `{t.path}` | {t.kind} | {t.cases} | {exercises} |")
        if not unit:
            lines += [
                "",
                "> No unit tests. Everything is asserted through the assembled "
                "application, so a failure localises poorly and edge cases that are "
                "hard to reach through HTTP are probably untested.",
            ]
        if not integration:
            lines += [
                "",
                "> No integration tests. Components are asserted in isolation, so "
                "nothing here would notice if they were wired together wrongly.",
            ]
    else:
        lines += [
            "**No tests were produced by this run.** Nothing below constitutes "
            "evidence that the deliverable behaves correctly.",
        ]

    lines += [
        "",
        "## Verification steps",
        "",
        "Each check runs against the files on disk. None of them asks an agent "
        "whether its own work was correct.",
        "",
        "| # | Check | What a pass establishes | What it cannot catch |",
        "| --- | --- | --- | --- |",
    ]
    for i, name in enumerate(check_names, start=1):
        proves, blind = CHECK_INTENT.get(
            name, ("—", "not documented; treat its result with care")
        )
        lines.append(f"| {i} | `{name}` | {proves} | {blind} |")

    lines += [
        "",
        "## Failure handling",
        "",
        "A failing check produces structured findings. A finding specific enough "
        "to describe its own fix is repaired and then **re-validated** — a repair "
        "is never trusted on the strength of having been applied. A finding that "
        "cannot describe a fix is escalated to a human rather than retried, and "
        "the repair budget is finite so a loop that is not converging stops "
        "instead of running forever.",
        "",
        "## What is not verified",
        "",
    ]

    not_verified = [
        "Performance. No load test runs, so the latency and throughput targets in "
        "the requirements are unmeasured — they are design intent, not results.",
        "Security. There is no dependency audit, no static security analysis and "
        "no penetration testing. The guardrail check refuses a list of known "
        "destructive operations; that is not a security review.",
        "Concurrency under real load. Thread-safety is argued for in the design and "
        "exercised only incidentally by the suite.",
        "Operational behaviour. Nothing is deployed, so migrations, startup, "
        "shutdown and failure modes are untested outside the test harness.",
    ]
    if requirement is not None and getattr(requirement, "out_of_scope", None):
        not_verified += [
            f"Out of scope by decision: {item}" for item in requirement.out_of_scope
        ]
    if api_contract is not None:
        not_verified.append(
            "Contract conformance is structural: the checks confirm each declared "
            "endpoint exists, and the tests assert the behaviour they happen to "
            "cover. Request and response bodies are not schema-validated against "
            "the published spec."
        )
    lines += [f"- {item}" for item in not_verified]
    lines += [""]
    return "\n".join(lines)


__all__ = ["CHECK_INTENT", "TestFile", "classify_tests", "render"]
