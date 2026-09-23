"""The checks that decide whether a run is trustworthy.

The one that matters most is `api_contract`: it is what notices that the
implementation quietly skipped an endpoint, and it is the source of the
`repair_hint` that lets the engine fix it rather than just complain.
"""

from __future__ import annotations

from asep.models import ApiContract, Artifact, ArtifactKind, CheckStatus, Endpoint, Severity
from asep.tools.workspace import Workspace
from asep.validation import (
    CheckContext,
    check_api_contract,
    check_guardrails,
    check_structure,
    check_syntax,
    check_tests,
    check_traceability,
)

ROUTES = '''from fastapi import APIRouter

router = APIRouter()


@router.get("/api/v1/urls/{code}")
def get_url(code: str):
    return {"code": code}
'''


def ctx_for(tmp_path, files: dict[str, str], **kwargs) -> CheckContext:
    ws = Workspace(tmp_path / "ws")
    for path, content in files.items():
        ws.write(path, content)
    return CheckContext(workspace=ws, run_tests=False, **kwargs)


class TestApiContract:
    def test_a_declared_endpoint_with_no_route_is_a_repairable_error(self, tmp_path):
        contract = ApiContract(
            title="t",
            endpoints=[
                Endpoint(method="GET", path="/api/v1/urls/{code}", summary="read", covers=["FR-001"]),
                Endpoint(
                    method="GET",
                    path="/api/v1/analytics/{code}",
                    summary="analytics",
                    covers=["FR-005"],
                    response_model="AnalyticsResponse",
                ),
            ],
        )
        result = check_api_contract(ctx_for(tmp_path, {"app/api/routes.py": ROUTES}, api_contract=contract))

        assert result.status is CheckStatus.FAIL
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert "/api/v1/analytics/{code}" in finding.message
        assert finding.repair_hint, "a finding without a hint cannot be repaired automatically"
        assert "AnalyticsResponse" in finding.repair_hint
        assert finding.requirement_id == "FR-005"

    def test_routes_match_by_shape_not_by_parameter_name(self, tmp_path):
        contract = ApiContract(
            title="t",
            endpoints=[
                Endpoint(method="GET", path="/api/v1/urls/{id}", summary="read", covers=["FR-001"])
            ],
        )
        result = check_api_contract(ctx_for(tmp_path, {"app/api/routes.py": ROUTES}, api_contract=contract))
        assert result.status is CheckStatus.PASS

    def test_it_skips_rather_than_passes_when_there_is_no_contract(self, tmp_path):
        result = check_api_contract(ctx_for(tmp_path, {"app/api/routes.py": ROUTES}))
        assert result.status is CheckStatus.SKIP


class TestStructureAndSyntax:
    def test_a_missing_required_file_fails(self, tmp_path):
        result = check_structure(ctx_for(tmp_path, {"app/main.py": "x = 1"}))
        assert result.status is CheckStatus.FAIL
        assert {f.target_path for f in result.findings} == {"requirements.txt", "README.md"}

    def test_unparseable_code_fails_with_a_line_number(self, tmp_path):
        result = check_syntax(ctx_for(tmp_path, {"app/broken.py": "def f(:\n    pass\n"}))
        assert result.status is CheckStatus.FAIL
        assert "app/broken.py:1" in result.findings[0].message

    def test_valid_code_passes(self, tmp_path):
        result = check_syntax(ctx_for(tmp_path, {"app/ok.py": "def f():\n    return 1\n"}))
        assert result.status is CheckStatus.PASS


class TestGuardrailCheck:
    def test_forbidden_content_on_disk_is_caught(self, tmp_path):
        result = check_guardrails(ctx_for(tmp_path, {"scripts/wipe.sh": "rm -rf /\n"}))
        assert result.status is CheckStatus.FAIL
        assert result.findings[0].target_path == "scripts/wipe.sh"

    def test_ordinary_code_passes(self, tmp_path):
        result = check_guardrails(ctx_for(tmp_path, {"app/ok.py": "x = 1\n"}))
        assert result.status is CheckStatus.PASS


class TestTraceability:
    def _requirement(self):
        from asep.models import (
            Clarity,
            FunctionalRequirement,
            NormalizedRequirement,
            RequirementKind,
        )

        return NormalizedRequirement(
            raw="r",
            intent="i",
            kind=RequirementKind.GREENFIELD,
            clarity=Clarity.CLEAR,
            functional=[
                FunctionalRequirement(id="FR-001", statement="covered"),
                FunctionalRequirement(id="FR-002", statement="orphaned"),
                FunctionalRequirement(id="FR-003", statement="optional", priority="could"),
            ],
        )

    def test_uncovered_must_have_requirements_warn_without_failing(self, tmp_path):
        artifact = Artifact(
            id="a", kind=ArtifactKind.CODE, path="a.py", content="x", produced_by="T-001",
            covers=["FR-001"],
        )
        result = check_traceability(
            ctx_for(tmp_path, {}, requirement=self._requirement(), artifacts={"a": artifact})
        )
        # A warning, not an error: this is a review signal, and failing the run
        # on it would make the platform refuse work a human would accept.
        assert result.status is CheckStatus.PASS
        assert [f.requirement_id for f in result.findings] == ["FR-002"]
        assert result.findings[0].severity is Severity.WARNING


class TestTestExecution:
    def test_a_passing_suite_passes(self, tmp_path):
        ctx = ctx_for(
            tmp_path,
            {"tests/test_ok.py": "def test_one():\n    assert True\n"},
        )
        ctx.run_tests = True
        result = check_tests(ctx)
        assert result.status is CheckStatus.PASS
        assert "1 passed" in result.detail

    def test_a_failing_test_becomes_a_finding_naming_the_file(self, tmp_path):
        ctx = ctx_for(
            tmp_path,
            {"tests/test_bad.py": "def test_one():\n    assert False\n"},
        )
        ctx.run_tests = True
        result = check_tests(ctx)
        assert result.status is CheckStatus.FAIL
        assert any("test_bad" in f.message for f in result.findings)

    def test_a_suite_that_cannot_run_is_a_failure_not_a_pass(self, tmp_path):
        ctx = ctx_for(tmp_path, {"app/main.py": "x = 1"})
        ctx.run_tests = True
        result = check_tests(ctx)
        # No tests found is not evidence of correctness.
        assert result.status is CheckStatus.FAIL

    def test_disabling_execution_skips_rather_than_passes(self, tmp_path):
        result = check_tests(ctx_for(tmp_path, {}))
        assert result.status is CheckStatus.SKIP
