from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class Finding(BaseModel):
    """A validation defect.

    `repair_hint` and `target_path` exist so the repair agent consumes structured
    findings rather than re-reading prose. A finding an agent cannot act on is a
    finding that will be reported forever.
    """

    check: str
    severity: Severity
    message: str
    target_path: str | None = None
    repair_hint: str | None = None
    requirement_id: str | None = None


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    findings: list[Finding] = []
    detail: str = ""
    duration_ms: int = 0


class ValidationReport(BaseModel):
    status: CheckStatus
    checks: list[CheckResult] = []
    attempt: int = 1

    @property
    def errors(self) -> list[Finding]:
        return [
            f
            for c in self.checks
            for f in c.findings
            if f.severity == Severity.ERROR
        ]

    @property
    def warnings(self) -> list[Finding]:
        return [
            f
            for c in self.checks
            for f in c.findings
            if f.severity == Severity.WARNING
        ]

    @property
    def repairable(self) -> list[Finding]:
        return [f for f in self.errors if f.repair_hint]

    @classmethod
    def from_checks(cls, checks: list[CheckResult], attempt: int = 1) -> ValidationReport:
        failed = any(c.status == CheckStatus.FAIL for c in checks)
        return cls(
            status=CheckStatus.FAIL if failed else CheckStatus.PASS,
            checks=checks,
            attempt=attempt,
        )
