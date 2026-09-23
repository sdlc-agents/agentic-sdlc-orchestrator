"""The HTTP layer is synthesised from the contract, not stored.

The claim these defend is narrow and checkable: change the approved contract
and the emitted module changes with it. A generator that ignores its input is
a fixture wearing a function's clothes, so most of this file feeds it altered
contracts and requires altered output.
"""

from __future__ import annotations

import ast

import pytest

from asep.models import ApiContract, Endpoint
from asep.providers.blueprints import url_shortener as bp
from asep.providers.responses import build
from asep.providers.responses.codegen import (
    OPERATIONS,
    implemented,
    render_routes,
    specificity,
    unimplemented,
)


@pytest.fixture
def contract() -> ApiContract:
    return build.api_contract()


def routes_of(module: str) -> list[tuple[str, str]]:
    """(method, path) for every route the emitted module declares, in order."""
    found = []
    for node in ast.walk(ast.parse(module)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                found.append((decorator.func.attr.upper(), decorator.args[0].value))
    return found


class TestItReproducesHandWrittenQuality:
    def test_the_output_is_byte_identical_to_the_module_it_replaced(self, contract):
        """If generation cost quality, it would not be worth doing."""
        assert render_routes(contract).strip() == bp.ROUTES.strip()

    def test_the_output_parses(self, contract):
        ast.parse(render_routes(contract))

    def test_definitions_are_separated_by_two_blank_lines(self, contract):
        lines = render_routes(contract).splitlines()
        for i, line in enumerate(lines):
            if not line.startswith("@router.") or i < 2:
                continue
            # A decorator may be preceded by an explanatory comment block.
            j = i
            while j > 0 and lines[j - 1].startswith("#"):
                j -= 1
            assert lines[j - 1] == "" and lines[j - 2] == "", (
                f"line {i + 1}: generated code should satisfy PEP 8"
            )


class TestItRespondsToTheContract:
    """The property that separates a generator from a stored string."""

    def test_removing_an_endpoint_removes_its_handler(self, contract):
        assert "def readyz" in render_routes(contract)

        trimmed = contract.model_copy(deep=True)
        trimmed.endpoints = [e for e in trimmed.endpoints if e.path != "/readyz"]

        module = render_routes(trimmed)
        assert "def readyz" not in module
        assert ("GET", "/readyz") not in routes_of(module)

    def test_changing_a_status_code_changes_the_decorator(self, contract):
        assert "status_code=201" in render_routes(contract)

        altered = contract.model_copy(deep=True)
        for endpoint in altered.endpoints:
            if endpoint.method == "POST" and endpoint.path == "/api/v1/urls":
                endpoint.status_codes = [202, 409, 422]

        module = render_routes(altered)
        assert "status_code=202" in module
        assert "status_code=201" not in module

    def test_renaming_a_path_parameter_keeps_the_handler(self, contract):
        """What a parameter is called is an authoring choice, not a new operation."""
        altered = contract.model_copy(deep=True)
        for endpoint in altered.endpoints:
            if endpoint.method == "GET" and endpoint.path == "/api/v1/urls/{code}":
                endpoint.path = "/api/v1/urls/{id}"

        module = render_routes(altered)
        assert ("GET", "/api/v1/urls/{id}") in routes_of(module)
        assert "def get_url" in module

    def test_moving_an_endpoint_to_a_new_path_reports_it_as_unimplemented(self, contract):
        """Deliberate: the generator will not assume an old handler still fits.

        A genuinely different path is a different operation. Reporting it as
        unimplemented is a true statement the contract check can act on;
        silently reusing the old body would be a guess.
        """
        altered = contract.model_copy(deep=True)
        for endpoint in altered.endpoints:
            if endpoint.path == "/healthz":
                endpoint.path = "/internal/health"

        assert "/internal/health" in [e.path for e in unimplemented(altered)]
        assert ("GET", "/healthz") not in routes_of(render_routes(altered))

    def test_a_redirect_does_not_get_a_redundant_status_code(self, contract):
        """3xx lives on the response object; pinning it twice is noise."""
        module = render_routes(contract)
        catch_all = [
            line for line in module.splitlines() if line.startswith('@router.get("/{code}"')
        ]
        assert catch_all and "status_code" not in catch_all[0]


class TestItEnforcesRulesTheContractDoesNotExpress:
    def test_the_catch_all_is_ordered_last_however_the_contract_lists_it(self, contract):
        """FastAPI matches in declaration order, so `/{code}` first eats /healthz."""
        reordered = contract.model_copy(deep=True)
        reordered.endpoints = list(reversed(reordered.endpoints))

        declared = routes_of(render_routes(reordered))
        assert declared[-1] == ("GET", "/{code}")

    def test_specificity_puts_literal_paths_before_parameterised_ones(self):
        literal = Endpoint(method="GET", path="/healthz", summary="x", covers=["FR-001"])
        catch_all = Endpoint(method="GET", path="/{code}", summary="x", covers=["FR-001"])
        assert specificity(literal) < specificity(catch_all)

    def test_only_the_imports_that_are_used_are_emitted(self, contract):
        """A module importing things it does not use is generated, not written."""
        minimal = contract.model_copy(deep=True)
        minimal.endpoints = [e for e in minimal.endpoints if e.path == "/healthz"]

        module = render_routes(minimal)
        assert "RedirectResponse" not in module
        assert "CodeUnavailable" not in module
        assert "HealthResponse" in module


class TestUnimplementedEndpoints:
    def test_an_endpoint_with_no_operation_is_left_out_not_stubbed(self, contract):
        """A stub would satisfy the contract check while lying about working."""
        missing = unimplemented(contract)
        assert [e.path for e in missing] == ["/api/v1/analytics/{code}"]

        module = render_routes(contract)
        assert "analytics" not in module.lower() or "get_analytics" not in module
        assert "NotImplementedError" not in module

    def test_the_gap_is_a_consequence_of_the_operation_library(self, contract):
        """Not a hand-written omission: the library simply has no entry."""
        assert ("GET", "/api/v1/analytics/{code}") not in OPERATIONS
        assert len(implemented(contract)) == len(contract.endpoints) - 1

    def test_the_contract_check_then_reports_it(self, tmp_path, contract):
        from asep.tools.workspace import Workspace
        from asep.validation import CheckContext, check_api_contract

        ws = Workspace(tmp_path / "ws")
        ws.write("app/api/routes.py", render_routes(contract))

        result = check_api_contract(
            CheckContext(workspace=ws, api_contract=contract, run_tests=False)
        )
        assert result.status.value == "fail"
        assert any("/api/v1/analytics/{code}" in f.message for f in result.findings)
