from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_SUMMARY = re.compile(
    r"(?:(\d+) failed)?[, ]*(?:(\d+) passed)?[, ]*(?:(\d+) skipped)?[, ]*(?:(\d+) error)?"
)


@dataclass
class TestRun:
    executed: bool
    exit_code: int
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    duration_ms: int = 0
    output: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.executed and self.failed == 0 and self.errors == 0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped

    def summary(self) -> str:
        if not self.executed:
            return f"not executed: {self.reason}"
        return (
            f"{self.passed} passed, {self.failed} failed, "
            f"{self.skipped} skipped in {self.duration_ms}ms"
        )


def _parse(output: str) -> tuple[int, int, int, int]:
    passed = failed = skipped = errors = 0
    for line in reversed(output.strip().splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            for count, label in re.findall(r"(\d+)\s+(passed|failed|skipped|errors?)", line):
                value = int(count)
                if label == "passed":
                    passed = value
                elif label == "failed":
                    failed = value
                elif label == "skipped":
                    skipped = value
                else:
                    errors = value
            if passed or failed or skipped or errors:
                break
    return passed, failed, skipped, errors


def run_pytest(cwd: Path, timeout_s: int = 180, args: list[str] | None = None) -> TestRun:
    """Execute the generated suite in a subprocess.

    A subprocess rather than an in-process call: generated code is untrusted, it
    imports modules the orchestrator has no business importing, and a hang must
    be bounded by a timeout rather than wedging the run.
    """
    cwd = Path(cwd)
    if not any(cwd.rglob("test_*.py")):
        return TestRun(executed=False, exit_code=-1, reason="no test files found")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(cwd)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", *(args or [])],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return TestRun(
            executed=False,
            exit_code=-1,
            duration_ms=int((time.perf_counter() - started) * 1000),
            reason=f"suite exceeded the {timeout_s}s budget and was terminated",
        )
    except FileNotFoundError as exc:
        return TestRun(executed=False, exit_code=-1, reason=f"python not runnable: {exc}")

    duration = int((time.perf_counter() - started) * 1000)
    output = (proc.stdout or "") + (proc.stderr or "")

    # Exit code 5 is "no tests collected"; 2+ generally means pytest itself broke.
    if proc.returncode == 5:
        return TestRun(
            executed=False,
            exit_code=5,
            duration_ms=duration,
            output=output[-4000:],
            reason="pytest collected no tests",
        )
    if "No module named pytest" in output:
        return TestRun(
            executed=False,
            exit_code=proc.returncode,
            duration_ms=duration,
            reason="pytest is not installed in this interpreter",
        )

    passed, failed, skipped, errors = _parse(output)
    return TestRun(
        executed=True,
        exit_code=proc.returncode,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
        duration_ms=duration,
        output=output[-6000:],
    )


def failing_tests(output: str) -> list[str]:
    return re.findall(r"^FAILED\s+(\S+)", output, flags=re.MULTILINE)
