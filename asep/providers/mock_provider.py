"""Deterministic provider, so the platform runs without an API key.

Content lives in `responses/`, one module per kind of work; this is only the
dispatcher. The build script deliberately omits an endpoint its contract
declares, so the repair loop has a real defect to find.
"""

from __future__ import annotations

import time

from pydantic import BaseModel

from .base import GenerationRequest, Provider
from .responses import SCRIPTS, ScenarioScript

# Kept as module-level names because scenarios, tests and docs refer to them.
GREENFIELD = "url_shortener"
BROWNFIELD = "analytics_upgrade"


class MockProvider(Provider):
    name = "mock"

    def __init__(self, latency_s: float = 0.0, fail_once: set[str] | None = None):
        self.latency_s = latency_s
        # Agents named here fail on their first attempt, to exercise the retry
        # path on demand rather than only when a real provider happens to fail.
        self.fail_once = set(fail_once or ())
        self._failed: set[str] = set()

    def describe(self) -> str:
        return "mock:deterministic-blueprint"

    def generate(self, request: GenerationRequest) -> BaseModel:
        from ..orchestration.errors import FatalError, TransientError

        if self.latency_s:
            time.sleep(self.latency_s)

        if request.agent in self.fail_once and request.agent not in self._failed:
            self._failed.add(request.agent)
            raise TransientError(
                f"mock provider: injected first-attempt failure for '{request.agent}'"
            )

        script: ScenarioScript | None = SCRIPTS.get(request.scenario)
        if script is None:
            raise FatalError(
                f"mock provider has no script for scenario '{request.scenario}' "
                f"(known: {sorted(SCRIPTS)})"
            )

        result = self._answer(script, request)
        if result is None:
            raise FatalError(
                f"scenario '{request.scenario}' has no answer for the '{request.agent}' "
                "stage; either the plan scheduled a stage this kind of change does "
                "not have, or the script is incomplete"
            )

        if not isinstance(result, request.schema):
            raise FatalError(
                f"mock provider produced {type(result).__name__} where "
                f"{request.schema.__name__} was requested by {request.agent}"
            )
        return result

    @staticmethod
    def _answer(script: ScenarioScript, request: GenerationRequest):
        agent = request.agent
        if agent == "requirement":
            return script.requirement(request.context.get("raw", ""))
        if agent == "architecture":
            return script.architecture() if script.architecture else None
        if agent == "api_design":
            return script.api_contract() if script.api_contract else None
        if agent == "planner":
            return script.plan()
        if agent == "implementation":
            return script.code() if script.code else None
        if agent == "test":
            return script.tests() if script.tests else None
        if agent == "documentation":
            return script.docs() if script.docs else None
        if agent == "repair":
            return script.repair(request) if script.repair else None
        return None


__all__ = ["BROWNFIELD", "GREENFIELD", "MockProvider"]
