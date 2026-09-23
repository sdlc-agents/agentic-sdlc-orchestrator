"""Regenerate the committed example runs under `examples/`.

Three runs, one per kind of input a reviewer should see: a greenfield
requirement, a brownfield change against a real codebase, and an ambiguous
requirement the platform refuses to guess at.

    python scripts/generate_examples.py

Workspaces are not committed — they are large and reproducible by running the
command each example prints. What is committed is the evidence: the trace, the
reports, and the console output.
"""

from __future__ import annotations

import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from asep.cli import make_observer, print_report  # noqa: E402
from asep.runner import RunConfig, run  # noqa: E402

EXAMPLES = pathlib.Path(__file__).resolve().parents[1] / "examples"

CASES = [
    (
        "greenfield",
        "python -m asep url_shortener",
        RunConfig(scenario="url_shortener"),
    ),
    (
        "brownfield",
        "python -m asep analytics_upgrade",
        RunConfig(scenario="analytics_upgrade"),
    ),
    (
        "ambiguous",
        "python -m asep url_shortener --no-assume",
        RunConfig(scenario="url_shortener", assume_defaults=False),
    ),
]


def _sanitise(folder: pathlib.Path) -> None:
    """Strip machine-specific paths out of committed artifacts.

    These files are documentation. A reader should see where a path sits in the
    project, not where the repository happened to live when it was generated.
    """
    root = str(EXAMPLES.parent)
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        cleaned = text.replace(root.replace("\\", "\\\\"), "<project>")
        cleaned = cleaned.replace(root, "<project>")
        cleaned = cleaned.replace(str(pathlib.Path.home()), "<home>")
        if cleaned != text:
            path.write_text(cleaned, encoding="utf-8")


class Tee:
    """Capture console output while still showing it."""

    def __init__(self, stream, buffer):
        self._stream = stream
        self._buffer = buffer

    def write(self, text):
        self._stream.write(text)
        self._buffer.append(text)
        return len(text)

    def flush(self):
        self._stream.flush()


def main() -> int:
    for name, command, config in CASES:
        target = EXAMPLES / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)

        config.out_dir = target / "_run"
        captured: list[str] = []
        original = sys.stdout
        sys.stdout = Tee(original, captured)  # type: ignore[assignment]
        try:
            result = run(config, observer=make_observer(quiet=True))
            print_report(result)
        finally:
            sys.stdout = original

        # Flatten the run directory up one level and drop the workspace.
        produced = result.run_dir
        for item in produced.iterdir():
            if item.name == "workspace":
                continue
            shutil.move(str(item), str(target / item.name))
        shutil.rmtree(config.out_dir)

        (target / "console.txt").write_text(
            f"$ {command}\n\n" + "".join(captured), encoding="utf-8"
        )
        _sanitise(target)
        print(f"wrote examples/{name}/  ({result.state.status.value})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
