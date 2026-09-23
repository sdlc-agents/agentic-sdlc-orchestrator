from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class GenerationRequest:
    agent: str
    task_id: str
    scenario: str
    instruction: str
    schema: type[BaseModel]
    context: dict[str, Any] = field(default_factory=dict)
    attempt: int = 1
    previous_error: str | None = None


class Provider(ABC):
    """Every model call in the system goes through this one method.

    Agents receive a validated Pydantic instance or an exception. They never see
    raw text, so a malformed model response is a typed failure the orchestrator
    can retry, not a parsing bug inside an agent.
    """

    name: str = "provider"

    @abstractmethod
    def generate(self, request: GenerationRequest) -> BaseModel: ...

    def describe(self) -> str:
        return self.name
