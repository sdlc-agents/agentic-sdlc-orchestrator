"""Checks run against what was actually produced.

A check reads the workspace, never an agent's claim about it. One that can
describe its own fix emits a `repair_hint`, which is what lets the engine
schedule a repair instead of only reporting a defect.
"""

from __future__ import annotations

import ast
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..models import (
    ApiContract,
    Architecture,
    Artifact,
    CheckResult,
    CheckStatus,
    Finding,
    NormalizedRequirement,
    Severity,
)
from ..orchestration.policy import Policy
from ..tools import failing_tests, run_pytest, scan
from ..tools.workspace import Workspace

REQUIRED_PATHS = ("app/main.py", "requirements.txt", "README.md")

SCREENABLE_SUFFIXES = {".py", ".sh", ".sql", ".yaml", ".yml", ".txt", ".md"}


@dataclass
class CheckContext:
    workspace: Workspace
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    requirement: NormalizedRequirement | None = None
    api_contract: ApiContract | None = None
    architecture: Architecture | None = None
    attempt: int = 1
    run_tests: bool = True
    test_timeout_s: int = 180
    # sha256 of every file that existed before the run touched anything. A
    # refactor is judged against this; a greenfield run has nothing to compare.
    baseline_digests: dict[str, str] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.workspace.root


def _normalize(path: str) -> str:
    """Compare routes by shape, not parameter name, so /a/{code} == /a/{id}."""
    parts = [
        "{}" if part.startswith("{") and part.endswith("}") else part
        for part in path.strip("/").split("/")
    ]
    return "/" + "/".join(parts)


def _timed(fn):
    def wrapper(ctx: CheckContext) -> CheckResult:
        started = time.perf_counter()
        result = fn(ctx)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ------------------------------------------------------------------- checks


@_timed
def check_structure(ctx: CheckContext) -> CheckResult:
    """The deliverable must be a runnable project, not a pile of snippets."""
    missing = [p for p in REQUIRED_PATHS if not ctx.workspace.exists(p)]
    findings = [
        Finding(
            check="structure",
            severity=Severity.ERROR,
            message=f"expected file is missing from the workspace: {path}",
            target_path=path,
        )
        for path in missing
    ]
    return CheckResult(
        name="structure",
        status=CheckStatus.FAIL if findings else CheckStatus.PASS,
        findings=findings,
        detail=(
            f"{len(REQUIRED_PATHS) - len(missing)}/{len(REQUIRED_PATHS)} "
            "required paths present"
        ),
    )


@_timed
def check_syntax(ctx: CheckContext) -> CheckResult:
    """Parse every generated module. Unparseable code is not a review problem."""
    findings: list[Finding] = []
    files = ctx.workspace.files("**/*.py")
    for path in files:
        rel = path.relative_to(ctx.root).as_posix()
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            findings.append(
                Finding(
                    check="syntax",
                    severity=Severity.ERROR,
                    message=f"{rel}:{exc.lineno}: {exc.msg}",
                    target_path=rel,
                )
            )
    return CheckResult(
        name="syntax",
        status=CheckStatus.FAIL if findings else CheckStatus.PASS,
        findings=findings,
        detail=f"parsed {len(files)} python file(s)",
    )


@_timed
def check_guardrails(ctx: CheckContext) -> CheckResult:
    """Re-screen what landed on disk.

    The engine screens artifacts as they are committed; this catches anything
    that reached the workspace by another path, so the guarantee is about the
    filesystem rather than about one code path being well behaved.
    """
    findings: list[Finding] = []
    for path in ctx.workspace.files("**/*"):
        if path.suffix not in SCREENABLE_SUFFIXES:
            continue
        rel = path.relative_to(ctx.root).as_posix()
        content = path.read_text(encoding="utf-8", errors="replace")
        for pattern in Policy.screen(content):
            findings.append(
                Finding(
                    check="guardrails",
                    severity=Severity.ERROR,
                    message=f"{rel} contains a forbidden operation matching {pattern!r}",
                    target_path=rel,
                )
            )
    return CheckResult(
        name="guardrails",
        status=CheckStatus.FAIL if findings else CheckStatus.PASS,
        findings=findings,
        detail="no forbidden operations found" if not findings else "policy violation",
    )


@_timed
def check_api_contract(ctx: CheckContext) -> CheckResult:
    """Every endpoint the contract promises must exist in the code.

    The hint is specific enough for the repair agent to act on without
    re-deriving the design.
    """
    if ctx.api_contract is None:
        return CheckResult(
            name="api_contract",
            status=CheckStatus.SKIP,
            detail="no api contract on the blackboard",
        )

    index = scan(ctx.root, include_tests=False)
    implemented = {
        (route.method.upper(), _normalize(route.path)) for _, route in index.routes
    }

    findings: list[Finding] = []
    for endpoint in ctx.api_contract.endpoints:
        if (endpoint.method.upper(), _normalize(endpoint.path)) in implemented:
            continue
        findings.append(
            Finding(
                check="api_contract",
                severity=Severity.ERROR,
                message=(
                    f"contract declares {endpoint.method} {endpoint.path} "
                    f"({endpoint.summary}) but no route implements it"
                ),
                target_path="app/api/routes.py",
                repair_hint=(
                    f"add a route handler for {endpoint.method} {endpoint.path} to "
                    f"app/api/routes.py returning "
                    f"{endpoint.response_model or 'the documented response model'}, "
                    "and import that model"
                ),
                requirement_id=endpoint.covers[0] if endpoint.covers else None,
            )
        )

    return CheckResult(
        name="api_contract",
        status=CheckStatus.FAIL if findings else CheckStatus.PASS,
        findings=findings,
        detail=(
            f"{len(ctx.api_contract.endpoints) - len(findings)}/"
            f"{len(ctx.api_contract.endpoints)} declared endpoints implemented"
        ),
    )


@_timed
def check_traceability(ctx: CheckContext) -> CheckResult:
    """Every must-have requirement should be claimed by some artifact.

    A warning rather than an error: missing coverage is a review signal, and
    failing the run on it would make the platform refuse work a human would
    accept.
    """
    if ctx.requirement is None:
        return CheckResult(
            name="traceability",
            status=CheckStatus.SKIP,
            detail="no normalized requirement on the blackboard",
        )

    covered = {rid for artifact in ctx.artifacts.values() for rid in artifact.covers}
    must = [r for r in ctx.requirement.functional if r.priority == "must"]
    findings = [
        Finding(
            check="traceability",
            severity=Severity.WARNING,
            message=f"{r.id} ({r.statement}) is not claimed by any artifact",
            requirement_id=r.id,
        )
        for r in must
        if r.id not in covered
    ]
    return CheckResult(
        name="traceability",
        status=CheckStatus.PASS,
        findings=findings,
        detail=f"{len(must) - len(findings)}/{len(must)} must-have requirements covered",
    )


@_timed
def check_tests(ctx: CheckContext) -> CheckResult:
    """Execute the generated suite. A test that was never run proves nothing."""
    if not ctx.run_tests:
        return CheckResult(
            name="tests", status=CheckStatus.SKIP, detail="test execution disabled"
        )

    run = run_pytest(ctx.root, timeout_s=ctx.test_timeout_s)
    if not run.executed:
        return CheckResult(
            name="tests",
            status=CheckStatus.FAIL,
            detail=run.summary(),
            findings=[
                Finding(
                    check="tests",
                    severity=Severity.ERROR,
                    message=f"the generated suite did not execute: {run.reason}",
                )
            ],
        )

    # pytest reports native separators; every other path in the system is
    # posix, and a report that mixes both is harder to read than it needs to be.
    findings = [
        Finding(
            check="tests",
            severity=Severity.ERROR,
            message=f"failing test: {name}",
            target_path=name.split("::")[0],
        )
        for name in (n.replace("\\", "/") for n in failing_tests(run.output))
    ]
    if run.failed and not findings:
        findings.append(
            Finding(
                check="tests",
                severity=Severity.ERROR,
                message=f"{run.failed} test(s) failed; see the captured pytest output",
            )
        )

    return CheckResult(
        name="tests",
        status=CheckStatus.FAIL if findings else CheckStatus.PASS,
        findings=findings,
        detail=run.summary(),
    )


@_timed
def check_behaviour_preserved(ctx: CheckContext) -> CheckResult:
    """For a refactor: the existing tests must still pass, *unedited*.

    A suite adjusted to fit the new structure proves nothing. The claim is that
    behaviour did not change, so the evidence is the original tests.
    """
    if not ctx.baseline_digests:
        return CheckResult(
            name="behaviour_preserved",
            status=CheckStatus.SKIP,
            detail="no pre-existing baseline; nothing to preserve",
        )

    findings: list[Finding] = []
    checked = 0
    for path, digest in sorted(ctx.baseline_digests.items()):
        if "test" not in Path(path).name:
            continue
        checked += 1
        if not ctx.workspace.exists(path):
            findings.append(
                Finding(
                    check="behaviour_preserved",
                    severity=Severity.ERROR,
                    message=f"{path} existed before the change and has been deleted",
                    target_path=path,
                )
            )
            continue
        # Raw bytes on both sides. Reading as text normalizes line endings, so
        # comparing decoded text against a byte-level baseline reports every
        # untouched file as modified on any platform that writes CRLF — a check
        # that cries wolf is worse than no check.
        current = hashlib.sha256(ctx.workspace.resolve(path).read_bytes()).hexdigest()
        if current != digest:
            findings.append(
                Finding(
                    check="behaviour_preserved",
                    severity=Severity.ERROR,
                    message=(
                        f"{path} was modified; a refactor that edits the tests "
                        "proving its behaviour has not preserved that behaviour"
                    ),
                    target_path=path,
                )
            )

    return CheckResult(
        name="behaviour_preserved",
        status=CheckStatus.FAIL if findings else CheckStatus.PASS,
        findings=findings,
        detail=f"{checked - len(findings)}/{checked} pre-existing test file(s) untouched",
    )


@_timed
def check_test_coverage(ctx: CheckContext) -> CheckResult:
    """For a test-improvement change: which source modules have no direct test?

    Deliberately crude — whether a module is imported by some test, not what
    fraction of its lines run. The useful signal is "nothing tests this at all".
    """
    sources = [
        p
        for p in ctx.workspace.files("**/*.py")
        if "test" not in p.name and p.name != "__init__.py" and "tests" not in p.parts
    ]
    if not sources:
        return CheckResult(
            name="test_coverage", status=CheckStatus.SKIP, detail="no source modules"
        )

    # Resolved from the import graph rather than by searching test text. A
    # substring search counts `state.cache.invalidate(...)` inside an API test
    # as coverage of the cache module, which credits a module nothing tests --
    # and a coverage check that over-reports is worse than none.
    index = scan(ctx.root, include_tests=True)
    test_modules = {
        m.path for m in index.modules if Path(m.path).name.startswith("test_")
    }

    findings: list[Finding] = []
    for source in sources:
        stem = source.stem
        rel = source.relative_to(ctx.root).as_posix()
        if set(index.importers_of(rel)) & test_modules:
            continue
        findings.append(
            Finding(
                check="test_coverage",
                severity=Severity.WARNING,
                message=f"no test file references {rel}",
                target_path=rel,
                repair_hint=(
                    f"add tests/test_{stem}.py exercising the public surface of {rel}"
                ),
            )
        )

    covered = len(sources) - len(findings)
    return CheckResult(
        name="test_coverage",
        # A warning: thin coverage is a review signal, not a broken build.
        status=CheckStatus.PASS,
        findings=findings,
        detail=(
            f"{covered}/{len(sources)} source module(s) imported by {len(test_modules)} "
            "test module(s)"
        ),
    )


@_timed
def check_documentation(ctx: CheckContext) -> CheckResult:
    """For a documentation change: is the published surface actually documented?

    Checked against the contract rather than against a word count, so "we wrote
    more docs" cannot pass while the endpoint a caller needs stays unmentioned.
    """
    docs = {
        p.relative_to(ctx.root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
        for p in ctx.workspace.files("**/*.md")
    }
    if not docs:
        return CheckResult(
            name="documentation",
            status=CheckStatus.FAIL,
            detail="no markdown documentation found",
            findings=[
                Finding(
                    check="documentation",
                    severity=Severity.ERROR,
                    message="the change claims to document the service but wrote no docs",
                )
            ],
        )

    corpus = "\n".join(docs.values())
    findings: list[Finding] = []
    if ctx.api_contract is not None:
        for endpoint in ctx.api_contract.endpoints:
            if endpoint.path in corpus:
                continue
            findings.append(
                Finding(
                    check="documentation",
                    severity=Severity.ERROR,
                    message=(
                        f"{endpoint.method} {endpoint.path} is part of the published "
                        "contract but appears in no document"
                    ),
                    repair_hint=(
                        f"document {endpoint.method} {endpoint.path} "
                        f"({endpoint.summary}) in the API reference"
                    ),
                )
            )

    return CheckResult(
        name="documentation",
        status=CheckStatus.FAIL if findings else CheckStatus.PASS,
        findings=findings,
        detail=f"{len(docs)} document(s); {len(findings)} undocumented endpoint(s)",
    )


DEFAULT_CHECKS: list[Callable[[CheckContext], CheckResult]] = [
    check_structure,
    check_syntax,
    check_guardrails,
    check_api_contract,
    check_traceability,
    check_tests,
]

# Checks a scenario can opt into by name, because they only mean something for
# a particular kind of change: preserving behaviour matters for a refactor,
# coverage for a test-improvement change, documentation for a docs change.
OPTIONAL_CHECKS: dict[str, Callable[[CheckContext], CheckResult]] = {
    "behaviour_preserved": check_behaviour_preserved,
    "test_coverage": check_test_coverage,
    "documentation": check_documentation,
}

# Retained for callers that want everything.
ALL_CHECKS: list[Callable[[CheckContext], CheckResult]] = [
    *DEFAULT_CHECKS,
    *OPTIONAL_CHECKS.values(),
]


def resolve_checks(extra: tuple[str, ...] | None = None):
    """The default battery, plus any optional checks a scenario asked for."""
    chosen = list(DEFAULT_CHECKS)
    for name in extra or ():
        try:
            chosen.append(OPTIONAL_CHECKS[name])
        except KeyError:
            raise KeyError(
                f"unknown check {name!r}; available: {sorted(OPTIONAL_CHECKS)}"
            ) from None
    return chosen


def run_checks(ctx: CheckContext, checks=None) -> list[CheckResult]:
    return [check(ctx) for check in (checks or DEFAULT_CHECKS)]
