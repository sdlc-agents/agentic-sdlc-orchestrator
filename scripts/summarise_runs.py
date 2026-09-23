"""Compare every run in a directory, as one markdown table.

Each run writes its own `metrics.json`. Read together they answer a question a
single run cannot: is this kind of work getting slower, retrying more, or
needing more repairs than the others?

    python scripts/summarise_runs.py runs/
    python scripts/summarise_runs.py runs/ >> "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def load(root: Path) -> list[dict]:
    runs = []
    for path in sorted(root.glob("*/metrics.json")):
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skipping {path}: {exc}", file=sys.stderr)
    return runs


def render(runs: list[dict]) -> str:
    if not runs:
        return "No run metrics found.\n"

    lines = [
        "## Run metrics",
        "",
        f"{len(runs)} run(s), newest first by scenario name.",
        "",
        "| Scenario | Result | First pass | Tasks | Repairs | Retries | Escalations | Checks | Artifacts | Time |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for run in sorted(runs, key=lambda r: r["scenario"]):
        tasks = run["tasks"]
        rel = run["reliability"]
        val = run["validation"]
        result = "pass" if run["status"] == "succeeded" else f"**{run['status']}**"
        checks = f"{val['checks_run'] - val['checks_failed']}/{val['checks_run']}"
        lines.append(
            f"| `{run['scenario']}` | {result} "
            f"| {'yes' if run['first_pass_yield'] else 'no'} "
            f"| {tasks['planned']}+{tasks['injected_at_runtime']} "
            f"| {rel['repair_rounds']} | {rel['retries']} | {rel['escalations']} "
            f"| {checks} | {run['output']['artifacts']} "
            f"| {run['duration_ms'] / 1000:.1f}s |"
        )

    first_pass = sum(1 for r in runs if r["first_pass_yield"])
    total_ms = sum(r["duration_ms"] for r in runs)
    lines += [
        "",
        f"**First-pass yield: {first_pass}/{len(runs)}** — runs that reached a "
        "passing verdict without repairing themselves. This moves before "
        "pass/fail does, because the repair loop can rescue a mediocre run.",
        "",
        f"Total wall clock: {total_ms / 1000:.1f}s.",
        "",
    ]

    # Where the time actually goes, summed across every run.
    spend: Counter[str] = Counter()
    calls: Counter[str] = Counter()
    for run in runs:
        for name, agent in run["agents"].items():
            spend[name] += agent["total_ms"]
            calls[name] += agent["invocations"]

    if spend:
        lines += [
            "<details><summary>Where the time went, across all runs</summary>",
            "",
            "| Agent | Calls | Total | Share |",
            "| --- | --- | --- | --- |",
        ]
        for name, ms in spend.most_common():
            share = 100 * ms / total_ms if total_ms else 0
            lines.append(f"| `{name}` | {calls[name]} | {ms / 1000:.1f}s | {share:.0f}% |")
        lines += ["", "</details>", ""]

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path("runs")
    if not root.is_dir():
        print(f"no such directory: {root}", file=sys.stderr)
        return 1
    print(render(load(root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
