"""Brownfield bug fix: expired links keep redirecting.

The defect is real and pre-existing, not planted for the demo. The read path is
cache-aside and the expiry check sits only on the cache-miss branch, so once a
link has been resolved once the cache answers on its behalf for as long as the
cache TTL lasts — well after the link itself has expired. The shipped suite
misses it because its expiry test never resolves the link before it expires, so
the cache is empty at the moment it checks.

This is also the scenario with a **well-defined** requirement: a reported defect
with observable symptoms leaves nothing blocking to ask about, which is what
makes it the counterpart to the greenfield run's three open questions.
"""

from __future__ import annotations

from ...agents.schemas import CodeBundle, GeneratedFile, TaskSpec, WorkPlan
from ...models import (
    Ambiguity,
    ApiContract,
    ArtifactKind,
    Clarity,
    Endpoint,
    FunctionalRequirement,
    NonFunctionalRequirement,
    NormalizedRequirement,
    RequirementKind,
    RiskLevel,
)

# The defective block, exactly as it stands in the target codebase. Used as the
# anchor for a surgical edit rather than rewriting the module around it.
BUGGY_READ_PATH = '''@router.get("/{code}")
def redirect(code: str, request: Request) -> RedirectResponse:
    cache = request.app.state.cache
    target = cache.get(code)

    if target is None:
        url = request.app.state.urls.get(code)
        if url is None or not url.resolvable:
            raise HTTPException(status_code=404, detail="unknown or expired code")
        target = url.long_url
        cache.set(code, target)
'''

FIXED_READ_PATH = '''def _entry_expired(entry: tuple[str, float | None]) -> bool:
    _, expires_at = entry
    return expires_at is not None and expires_at <= time.time()


@router.get("/{code}")
def redirect(code: str, request: Request) -> RedirectResponse:
    cache = request.app.state.cache
    entry = cache.get(code)

    # A cached entry now carries the link's own expiry alongside its target.
    # Without it the cache answers on behalf of a link that has already expired
    # and keeps doing so until the cache TTL lapses, because the read path never
    # consults the repository on a hit and nothing downstream can notice.
    if entry is not None and _entry_expired(entry):
        cache.invalidate(code)
        entry = None

    if entry is None:
        url = request.app.state.urls.get(code)
        if url is None or not url.resolvable:
            raise HTTPException(status_code=404, detail="unknown or expired code")
        entry = (url.long_url, url.expires_at)
        cache.set(code, entry)

    target = entry[0]
'''

REGRESSION_TEST = '''"""Regression tests for the expired-link defect.

An expired link kept redirecting. The read path is cache-aside and the expiry
check lived only on the cache-miss branch, so once a link had been resolved once
the cache answered on its behalf for as long as the cache TTL lasted -- long
after the link itself had expired.

The existing suite misses this because its expiry test never resolves the link
before it expires, so the cache is empty when the expired lookup happens. These
tests resolve first, which is what a real visitor does.
"""

from __future__ import annotations

import time

TARGET = "https://example.com/expiring"


def _short_lived(client, seconds: float):
    """A link that expires by the ordinary passage of time, not by mutation."""
    return client.app.state.urls.create(
        long_url=TARGET, expires_at=time.time() + seconds
    )


def test_expired_link_stops_resolving_even_when_already_cached(client):
    url = _short_lived(client, 0.4)

    assert client.get("/" + url.code, follow_redirects=False).status_code == 302

    time.sleep(0.5)

    assert client.get("/" + url.code, follow_redirects=False).status_code == 404


def test_the_repository_and_the_read_path_agree_about_expiry(client):
    url = _short_lived(client, 0.4)
    client.get("/" + url.code, follow_redirects=False)

    time.sleep(0.5)

    assert client.app.state.urls.get(url.code).resolvable is False
    assert client.get("/" + url.code, follow_redirects=False).status_code == 404


def test_a_live_link_is_still_served_from_the_cache(client):
    """The fix must not turn every cache hit into a database read."""
    url = _short_lived(client, 3600)
    client.get("/" + url.code, follow_redirects=False)

    before = client.app.state.cache.hits
    assert client.get("/" + url.code, follow_redirects=False).status_code == 302
    assert client.app.state.cache.hits == before + 1
'''


def requirement(raw: str) -> NormalizedRequirement:
    return NormalizedRequirement(
        raw=raw,
        intent=(
            "A reported defect: links that have passed their expiry keep serving "
            "redirects instead of returning 404. Reproduce it, fix it, and prove "
            "the fix without slowing the read path down."
        ),
        kind=RequirementKind.BROWNFIELD,
        # Well-defined: a defect report with an observable symptom and a stated
        # expected behaviour leaves nothing blocking to ask.
        clarity=Clarity.CLEAR,
        functional=[
            FunctionalRequirement(
                id="FR-001",
                statement=(
                    "A link that has passed its expiry must stop resolving immediately, "
                    "including when it was resolved before it expired"
                ),
            ),
            FunctionalRequirement(
                id="FR-002",
                statement=(
                    "A regression test must fail against the unfixed code, so the fix "
                    "is verified against a reproduced defect rather than asserted"
                ),
            ),
            FunctionalRequirement(
                id="FR-003",
                statement="The repository and the read path must agree about expiry",
                priority="should",
            ),
        ],
        non_functional=[
            NonFunctionalRequirement(
                id="NFR-001",
                category="performance",
                statement=(
                    "The fix must not add a database read to the cache-hit path; "
                    "correctness must not be bought by removing the cache"
                ),
                target="a cache hit stays a cache hit",
            ),
            NonFunctionalRequirement(
                id="NFR-002",
                category="maintainability",
                statement="The existing suite must keep passing without edits",
            ),
        ],
        ambiguities=[
            Ambiguity(
                id="AMB-001",
                question=(
                    "Should the cache carry each entry's expiry, or should entries be "
                    "evicted at the moment a link expires?"
                ),
                why_it_matters=(
                    "Eviction-on-expiry needs a scheduler or a write path that knows "
                    "every cache; carrying the expiry keeps the change inside the "
                    "read path."
                ),
                # Not blocking: both answers fix the defect, and one is clearly
                # cheaper, so this does not need a person to unblock it.
                blocking=False,
                default_assumption=(
                    "Carry the expiry on the cached entry and drop it lazily on read"
                ),
            )
        ],
        out_of_scope=[
            "Reclaiming storage for expired links",
            "Making expiry precise across multiple instances with separate caches",
            "Changing the default cache TTL",
        ],
    )


def api_contract() -> ApiContract:
    """The contract the defect violates.

    A bug fix does not design a new surface; it restates the promise the code is
    breaking, so the validation stage can hold the fix to it.
    """
    return ApiContract(
        title="URL Shortener — redirect contract",
        version="1.0.1",
        endpoints=[
            Endpoint(
                method="GET",
                path="/{code}",
                summary="Resolve a live short code, or 404 once it has expired",
                covers=["FR-001", "FR-003"],
                status_codes=[302, 404],
            )
        ],
    )


def plan() -> WorkPlan:
    return WorkPlan(
        strategy=(
            "Reproduce before repairing. The regression test is written first and "
            "run against the unfixed code, and the run stops if it passes — a test "
            "that was green before the change proves nothing about the change. Only "
            "then is the fix applied, and the same test re-run. There is no "
            "architecture stage: the defect is a missing check on an existing path, "
            "and inventing a design document for it would be ceremony."
        ),
        parallelism_note=(
            "Nothing here is parallel. Reproduce, fix, verify is a strict chain, and "
            "pretending otherwise would let the fix land before the evidence."
        ),
        tasks=[
            TaskSpec(
                id="T-010",
                title="Write a regression test that reproduces the defect",
                agent="test",
                depends_on=[],
                covers=["FR-002"],
                reads=["requirement", "impact_analysis"],
                writes=["test_suite_index"],
                rationale="Written against the reported behaviour, not against the fix",
            ),
            TaskSpec(
                id="T-020",
                title="Run the regression test against the unfixed code and require it to fail",
                agent="reproduction",
                depends_on=["T-010"],
                covers=["FR-002"],
                reads=["test_suite_index"],
                writes=["reproduction"],
                rationale=(
                    "If it passes here, the defect is not what was reported and a "
                    "patch would be guesswork"
                ),
            ),
            TaskSpec(
                id="T-030",
                title="Fix the read path so a cached entry cannot outlive its link",
                agent="implementation",
                depends_on=["T-020"],
                covers=["FR-001", "FR-003"],
                reads=["api_contract", "reproduction"],
                writes=["artifacts_index", "implementation_summary"],
                risk=RiskLevel.MEDIUM,
                rationale="Edits existing source, so it is gated for human approval",
            ),
            TaskSpec(
                id="T-040",
                title="Re-run the full suite and check the contract",
                agent="validation",
                depends_on=["T-030"],
                reads=["artifacts_index"],
                writes=["validation_report"],
                rationale="The same test that failed must now pass, and nothing else may break",
            ),
            TaskSpec(
                id="T-050",
                title="Summarize the fix for a reviewer",
                agent="summary",
                depends_on=["T-040"],
                reads=["requirement"],
                writes=["run_summary"],
            ),
        ],
    )


def tests() -> CodeBundle:
    return CodeBundle(
        summary=(
            "Added a regression test that resolves a link before it expires, which is "
            "the sequence the existing suite never exercises and the only one that "
            "reaches the defect."
        ),
        files=[
            GeneratedFile(
                path="tests/test_expiry_regression.py",
                content=REGRESSION_TEST,
                kind=ArtifactKind.TEST,
                covers=["FR-001", "FR-002", "FR-003"],
            )
        ],
        notes=[
            "The link is expired by letting real time pass, not by editing the "
            "database, so the test exercises the defect rather than a simulation of it.",
            "A third test asserts a live link is still served from the cache, so the "
            "obvious wrong fix — consult the repository on every request — fails.",
        ],
    )


def code() -> CodeBundle:
    return CodeBundle(
        summary=(
            "Cached entries now carry the link's expiry alongside its target, and a "
            "stale entry is dropped on read. One function changed; the cache-hit path "
            "still never touches the database."
        ),
        files=[
            GeneratedFile(
                path="app/api/routes.py",
                content=FIXED_READ_PATH,
                replaces=BUGGY_READ_PATH,
                kind=ArtifactKind.CODE,
                covers=["FR-001", "FR-003"],
            )
        ],
        notes=[
            "Expressed as an anchored edit rather than a rewritten module, so the "
            "diff a reviewer sees is the fix and nothing else.",
            "The alternative — evicting on expiry — needs a scheduler and a cache "
            "the write path knows about; this keeps the change inside the read path.",
        ],
    )


__all__ = ["api_contract", "code", "plan", "requirement", "tests"]
