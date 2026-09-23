from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel


class ArtifactKind(str, Enum):
    CODE = "code"
    TEST = "test"
    API_SPEC = "api_spec"
    SCHEMA = "schema"
    MIGRATION = "migration"
    DOC = "doc"
    REPORT = "report"
    PATCH = "patch"


class Artifact(BaseModel):
    id: str
    kind: ArtifactKind
    path: str
    content: str
    produced_by: str
    covers: list[str] = []
    language: str | None = None
    supersedes: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def lines(self) -> int:
        return self.content.count("\n") + 1
