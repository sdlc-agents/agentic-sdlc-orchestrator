from __future__ import annotations

import shutil
from pathlib import Path

from ..orchestration.errors import GuardrailViolation


class Workspace:
    """Every file an agent writes goes through here.

    Absolute paths, parent traversal and symlinks leaving the root are rejected
    before any write. This is the capability boundary: approval controls whether
    work happens, this controls where it can land.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or candidate.drive or relative.startswith(("/", "\\")):
            raise GuardrailViolation(f"absolute path rejected: {relative}")
        if ".." in candidate.parts:
            raise GuardrailViolation(f"parent traversal rejected: {relative}")

        target = (self.root / candidate).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            raise GuardrailViolation(
                f"path escapes the workspace: {relative}"
            ) from None
        return target

    def write(self, relative: str, content: str) -> Path:
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read(self, relative: str) -> str:
        return self.resolve(relative).read_text(encoding="utf-8")

    def exists(self, relative: str) -> bool:
        try:
            return self.resolve(relative).exists()
        except GuardrailViolation:
            return False

    def files(self, pattern: str = "**/*") -> list[Path]:
        return sorted(p for p in self.root.glob(pattern) if p.is_file())

    def relative_files(self, pattern: str = "**/*") -> list[str]:
        return [p.relative_to(self.root).as_posix() for p in self.files(pattern)]

    def reset(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
