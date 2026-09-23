"""Deterministic answers, one module per kind of engineering work.

The mock provider needs a response for every agent in every scenario. Keeping
those responses beside each other in one file made the build scenario readable
and everything else an afterthought, so each kind of work gets its own module
and states its own shape: what a bug fix needs is not what a refactor needs, and
pretending otherwise is how a platform ends up only really supporting the
scenario it was demoed with.

**Naming rule.** Each module is named for the work kind it answers for, matching
`Scenario.work` exactly. `test_naming_conventions.py` enforces it, so the two
cannot drift apart:

| Module | `Scenario.work` | Scenario |
| --- | --- | --- |
| `build.py` | `build` | `url_shortener` |
| `enhancement.py` | `enhancement` | `analytics_upgrade` |
| `bug_fix.py` | `bug_fix` | `fix_expiry_bug` |
| `refactor.py` | `refactor` | `extract_service_layer` |
| `test_improvement.py` | `test_improvement` | `raise_test_coverage` |
| `documentation.py` | `documentation` | `document_the_service` |

`build` is what the documentation calls a *greenfield* run. The two words are
not synonyms: `RequirementKind` says whether a codebase already exists, `work`
says what kind of job is being done, and a module answers for the job.

A script supplies only the stages its scenario actually runs. A test-improvement
change has no architecture stage, and asking for one returns nothing rather than
inventing a design nobody needed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import bug_fix, build, documentation, enhancement, refactor, test_improvement


@dataclass(frozen=True)
class ScenarioScript:
    """What the provider can answer for one scenario.

    Every field is a callable rather than a value so nothing is constructed
    until an agent asks for it, and so two runs never share a mutable object.
    """

    requirement: Callable
    plan: Callable
    architecture: Callable | None = None
    api_contract: Callable | None = None
    code: Callable | None = None
    tests: Callable | None = None
    docs: Callable | None = None
    repair: Callable | None = None


SCRIPTS: dict[str, ScenarioScript] = {
    "url_shortener": ScenarioScript(
        requirement=build.requirement,
        architecture=build.architecture,
        api_contract=build.api_contract,
        plan=build.plan,
        code=build.code,
        tests=build.tests,
        docs=build.docs,
        repair=build.repair,
    ),
    "analytics_upgrade": ScenarioScript(
        requirement=enhancement.requirement,
        architecture=enhancement.architecture,
        api_contract=enhancement.api_contract,
        plan=enhancement.plan,
        code=enhancement.code,
        tests=enhancement.tests,
        docs=enhancement.docs,
        repair=build.repair,
    ),
    "fix_expiry_bug": ScenarioScript(
        requirement=bug_fix.requirement,
        api_contract=bug_fix.api_contract,
        plan=bug_fix.plan,
        code=bug_fix.code,
        tests=bug_fix.tests,
    ),
    "extract_service_layer": ScenarioScript(
        requirement=refactor.requirement,
        architecture=refactor.architecture,
        api_contract=refactor.api_contract,
        plan=refactor.plan,
        code=refactor.code,
    ),
    "raise_test_coverage": ScenarioScript(
        requirement=test_improvement.requirement,
        plan=test_improvement.plan,
        tests=test_improvement.tests,
    ),
    "document_the_service": ScenarioScript(
        requirement=documentation.requirement,
        api_contract=documentation.api_contract,
        plan=documentation.plan,
        docs=documentation.docs,
    ),
}

__all__ = ["SCRIPTS", "ScenarioScript"]
