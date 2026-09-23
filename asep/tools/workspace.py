from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath

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
        # Judge the path under both conventions. `Path` follows the host OS, so
        # on Linux "C:/Windows/x" is just a directory called "C:" and a
        # Windows-style path would be accepted; on Windows a backslash path
        # splits but on Linux it does not. A guard that depends on the host is
        # not a guard — the same generated path must be refused everywhere.
        posix = PurePosixPath(relative)
        windows = PureWindowsPath(relative)

        if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
            raise GuardrailViolation(f"absolute path rejected: {relative}")
        if ".." in posix.parts or ".." in windows.parts:
            raise GuardrailViolation(f"parent traversal rejected: {relative}")

        # Normalise separators so a path means the same thing on either host.
        candidate = Path(*PurePosixPath(relative.replace("\\", "/")).parts)

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
