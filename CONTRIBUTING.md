# Contributing

## Branching

Trunk-based, with short-lived branches off `main`. Not GitFlow — there are no
release trains or long-lived support versions here, and a `develop` branch
would be ceremony that buys nothing.

```
main ─────●────────────●────────────●──────▶   always green, always deployable
           \          /  \         /
            ●────────●    ●───────●             short-lived, hours to days
            feat/…        fix/…
```

`main` is protected: it takes changes through a pull request, and CI must pass
before merge.

### Branch names

`<type>/<short-description>`, kebab-case.

| Prefix | For |
| --- | --- |
| `feat/` | new capability — an agent, a scenario, a check |
| `fix/` | a defect in existing behaviour |
| `refactor/` | restructuring with no behaviour change |
| `test/` | tests only, no production code |
| `docs/` | documentation only |
| `ci/` | build, workflow or tooling |

Examples: `feat/security-review-agent`, `fix/workspace-host-independence`,
`ci/split-lint-and-coverage-jobs`.

### Keep branches short

Rebase on `main` rather than merging it in, so history stays linear and a
branch's diff shows only that branch's work:

```bash
git fetch origin && git rebase origin/main
```

A branch open longer than a few days is usually a sign the change should have
been split.

## Commits

Subject in the imperative, under ~70 characters, no trailing full stop. The
body explains **why**, since the diff already shows what:

```
Make the workspace guard independent of the host OS

CI caught this on Linux: `Workspace.resolve` refused "C:/Windows/bad.py" on
Windows but accepted it on Linux, where `Path` is a PosixPath and a drive
letter is just an unusual directory name.
```

A commit that needs no explanation needs no body. One that changes behaviour
almost always does.

## Pull requests

Say what changed and what would have to be true for it to be wrong. If the
change fixes a defect, the PR should contain the test that fails without it.

Squash on merge. Branch history is working notes; `main` history is the record.

## Before you push

```bash
pytest -q -m "not slow"              # ~9s, the edit-run loop
pytest -q                            # ~150s, everything
ruff check asep tests scripts
coverage run --source=asep -m pytest && coverage report
```

CI runs all of this plus every scenario end to end on Ubuntu and Windows across
Python 3.10 and 3.12. Anything that only breaks on another platform is exactly
what the matrix is for — one such bug has already shipped and been caught there.

## What CI enforces

| Job | Gate |
| --- | --- |
| `lint` | ruff clean |
| `security` | `pip-audit` finds no known vulnerability |
| `test` (×4) | full suite on Ubuntu + Windows, Python 3.10 + 3.12 |
| `coverage` | ≥ 90% of `asep/`, enforced not reported |
| `scenarios` | all six scenarios exit zero; sample codebase and examples regenerate byte-identically |

The reproducibility checks matter: `sample_codebase/` is derived from the
blueprint and `examples/` from real runs, so both drift silently if nothing
regenerates them. CI fails on any diff.

## Adding to the platform

See [`docs/extending.md`](docs/extending.md) for adding a validation check, an
agent, a scenario or a provider — including the conventions
`tests/test_naming_conventions.py` enforces.
