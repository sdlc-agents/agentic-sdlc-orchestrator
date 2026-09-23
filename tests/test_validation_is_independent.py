"""Adversarial check: validation must catch defects nobody told it about.

The seeded analytics gap is the defect the platform was built around, so it
proves less than it appears to — a check hard-coded to look for exactly that
would pass the end-to-end tests. These take a workspace that has already
passed, break it in ways the checks were not written against, and require a
FAIL each time.

If this file ever goes green while the checks are disabled, the validation stage
is decoration and every "succeeded" the platform prints is worthless.
"""

from __future__ import annotations

import shutil

import pytest

from asep.models import CheckStatus
from asep.providers.responses import build
from asep.runner import RunConfig, run
from asep.tools.workspace import Workspace
from asep.validation import (
    CheckContext,
    check_api_contract,
    check_guardrails,
    check_structure,
    check_syntax,
    check_tests,
    run_checks,
)

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def passing_workspace(tmp_path_factory):
    """A real deliverable from a real run, verified clean before it is broken."""
    out = tmp_path_factory.mktemp("adversarial")
    result = run(RunConfig(scenario="url_shortener", out_dir=out))
    assert result.ok, "the baseline run must succeed or the premise is gone"
    return result.workspace


@pytest.fixture
def broken(passing_workspace, tmp_path):
    """A disposable copy of the clean workspace, ready to be sabotaged."""
    target = tmp_path / "ws"
    shutil.copytree(
        passing_workspace, target, ignore=shutil.ignore_patterns("__pycache__", "*.db")
    )
    return Workspace(target)


def context(workspace, run_tests=False):
    return CheckContext(
        workspace=workspace,
        api_contract=build.api_contract(),
        requirement=build.requirement("x"),
        run_tests=run_tests,
        test_timeout_s=120,
    )


def test_the_baseline_really_is_clean(passing_workspace):
    """Otherwise every assertion below passes for the wrong reason."""
    checks = run_checks(context(Workspace(passing_workspace), run_tests=True))
    failing = [c.name for c in checks if c.status is CheckStatus.FAIL]
    assert failing == []


class TestInjectedDefects:
    def test_an_endpoint_quietly_dropped_from_the_code_is_caught(self, broken):
        body = broken.read("app/api/routes.py")
        start = body.index('@router.delete("/api/v1/urls/{code}"')
        end = body.index("@router.get(", start + 10)
        broken.write("app/api/routes.py", body[:start] + body[end:])

        result = check_api_contract(context(broken))
        assert result.status is CheckStatus.FAIL
        assert any("DELETE /api/v1/urls/{code}" in f.message for f in result.findings)

    def test_code_that_is_syntactically_valid_but_behaviourally_wrong_is_caught(self, broken):
        """A 301 parses fine, satisfies the contract, and silently kills analytics.

        Only executing the suite finds this, which is why the tests check is not
        optional for a trustworthy verdict.
        """
        body = broken.read("app/api/routes.py")
        broken.write("app/api/routes.py", body.replace("status_code=302", "status_code=301", 1))

        assert check_syntax(context(broken)).status is CheckStatus.PASS
        assert check_api_contract(context(broken)).status is CheckStatus.PASS

        result = check_tests(context(broken, run_tests=True))
        assert result.status is CheckStatus.FAIL
        assert any("test_redirect_is_302" in f.message for f in result.findings)

    def test_unparseable_code_is_caught(self, broken):
        broken.write("app/cache.py", broken.read("app/cache.py") + "\ndef broken(:\n    pass\n")
        result = check_syntax(context(broken))
        assert result.status is CheckStatus.FAIL
        assert any(f.target_path == "app/cache.py" for f in result.findings)

    def test_a_forbidden_operation_smuggled_onto_disk_is_caught(self, broken):
        broken.write("scripts/deploy.sh", "#!/bin/sh\nkubectl delete deployment urls\n")
        result = check_guardrails(context(broken))
        assert result.status is CheckStatus.FAIL
        assert any(f.target_path == "scripts/deploy.sh" for f in result.findings)

    def test_a_deleted_project_file_is_caught(self, broken):
        (broken.root / "requirements.txt").unlink()
        result = check_structure(context(broken))
        assert result.status is CheckStatus.FAIL
        assert any(f.target_path == "requirements.txt" for f in result.findings)

    def test_a_deleted_test_suite_does_not_read_as_success(self, broken):
        """The most dangerous failure mode: no tests ran, so nothing failed."""
        shutil.rmtree(broken.root / "tests")
        result = check_tests(context(broken, run_tests=True))
        assert result.status is CheckStatus.FAIL, (
            "a suite that never executed is not evidence of correctness"
        )
