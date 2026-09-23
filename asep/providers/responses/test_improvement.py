"""Test improvement: put the untested modules under test.

The cache and the repository were only ever exercised through HTTP, which means
their own behaviour was never asserted — an LRU that never evicted, or a
`purge_expired` that deactivated everything, would have passed the entire suite.
Coverage through the front door is coverage of the front door.

This scenario has no architecture or API design stage. Nothing about the system
is being designed, and generating a design document to justify writing tests
would be ceremony rather than engineering.
"""

from __future__ import annotations

from ...agents.schemas import CodeBundle, GeneratedFile, TaskSpec, WorkPlan
from ...models import (
    Ambiguity,
    ArtifactKind,
    Clarity,
    FunctionalRequirement,
    NonFunctionalRequirement,
    NormalizedRequirement,
    RequirementKind,
)

TEST_CACHE = '''"""Direct tests for the read-path cache.

The cache was previously exercised only through the API, which means its
eviction and expiry behaviour was never actually asserted -- an LRU that never
evicted would have passed the whole suite.
"""

from __future__ import annotations

import time

import pytest

from app.cache import LruTtlCache


@pytest.fixture
def cache() -> LruTtlCache:
    return LruTtlCache(capacity=3, ttl_s=10.0)


def test_a_stored_value_is_returned(cache):
    cache.set("a", "https://example.com")
    assert cache.get("a") == "https://example.com"


def test_a_missing_key_returns_none_and_counts_a_miss(cache):
    assert cache.get("nope") is None
    assert cache.misses == 1
    assert cache.hits == 0


def test_hits_and_misses_are_counted_separately(cache):
    cache.set("a", 1)
    cache.get("a")
    cache.get("a")
    cache.get("b")
    assert (cache.hits, cache.misses) == (2, 1)


def test_capacity_is_enforced_by_evicting_the_least_recently_used(cache):
    for key in ("a", "b", "c"):
        cache.set(key, key)
    cache.get("a")          # 'a' is now the most recently used, 'b' the least
    cache.set("d", "d")     # forces one eviction

    assert cache.get("b") is None, "the least recently used entry should be gone"
    assert cache.get("a") == "a"
    assert cache.get("d") == "d"


def test_an_entry_expires_after_its_ttl():
    cache = LruTtlCache(capacity=10, ttl_s=0.2)
    cache.set("a", "value")
    assert cache.get("a") == "value"
    time.sleep(0.25)
    assert cache.get("a") is None


def test_invalidate_removes_an_entry(cache):
    cache.set("a", "value")
    cache.invalidate("a")
    assert cache.get("a") is None


def test_invalidating_an_absent_key_is_not_an_error(cache):
    cache.invalidate("never-existed")


def test_overwriting_a_key_keeps_one_entry(cache):
    cache.set("a", 1)
    cache.set("a", 2)
    assert cache.get("a") == 2
'''

TEST_REPOSITORY = '''"""Direct tests for persistence.

Collision retry, expiry and deactivation were only ever reachable through HTTP,
so their edge cases went unasserted.
"""

from __future__ import annotations

import time

import pytest

from app.db import Database
from app.repository import CodeUnavailable, UrlRepository

TARGET = "https://example.com/target"


@pytest.fixture
def repo(tmp_path) -> UrlRepository:
    db = Database(str(tmp_path / "repo.db"))
    db.init_schema()
    try:
        yield UrlRepository(db)
    finally:
        db.close()


def test_a_created_link_can_be_read_back(repo):
    url = repo.create(long_url=TARGET)
    assert repo.get(url.code).long_url == TARGET


def test_an_unknown_code_returns_none(repo):
    assert repo.get("nosuchcode") is None


def test_a_custom_alias_is_honoured(repo):
    url = repo.create(long_url=TARGET, code="mycode")
    assert url.code == "mycode"


def test_a_taken_alias_is_refused(repo):
    repo.create(long_url=TARGET, code="taken")
    with pytest.raises(CodeUnavailable):
        repo.create(long_url=TARGET, code="taken")


def test_generated_codes_are_distinct(repo):
    codes = {repo.create(long_url=TARGET).code for _ in range(25)}
    assert len(codes) == 25


def test_an_expired_link_is_not_resolvable(repo):
    url = repo.create(long_url=TARGET, expires_at=time.time() - 1)
    assert repo.get(url.code).resolvable is False


def test_a_future_expiry_is_still_resolvable(repo):
    url = repo.create(long_url=TARGET, expires_at=time.time() + 3600)
    assert repo.get(url.code).resolvable is True


def test_deactivating_makes_a_link_unresolvable_without_deleting_it(repo):
    url = repo.create(long_url=TARGET)
    assert repo.deactivate(url.code) is True
    stored = repo.get(url.code)
    assert stored is not None, "deactivation should not destroy the record"
    assert stored.resolvable is False


def test_deactivating_an_unknown_code_reports_false(repo):
    assert repo.deactivate("nosuchcode") is False


def test_purge_expired_deactivates_only_expired_links(repo):
    live = repo.create(long_url=TARGET, expires_at=time.time() + 3600)
    dead = repo.create(long_url=TARGET, expires_at=time.time() - 1)

    assert repo.purge_expired() == 1
    assert repo.get(dead.code).resolvable is False
    assert repo.get(live.code).resolvable is True


def test_owner_is_recorded_when_supplied(repo):
    url = repo.create(long_url=TARGET, owner="team-a")
    assert repo.get(url.code).owner == "team-a"
'''


def requirement(raw: str) -> NormalizedRequirement:
    return NormalizedRequirement(
        raw=raw,
        intent=(
            "The cache and the repository have no tests of their own. Put their "
            "behaviour under direct test, including the edge cases that only "
                "appear under eviction, expiry and alias collision."
        ),
        kind=RequirementKind.BROWNFIELD,
        clarity=Clarity.CLEAR,
        functional=[
            FunctionalRequirement(
                id="FR-001",
                statement="The cache has direct tests for hits, misses, eviction and TTL expiry",
            ),
            FunctionalRequirement(
                id="FR-002",
                statement=(
                    "The repository has direct tests for creation, alias collision, "
                    "expiry, deactivation and purge"
                ),
            ),
            FunctionalRequirement(
                id="FR-003",
                statement="No production code changes as part of this work",
            ),
        ],
        non_functional=[
            NonFunctionalRequirement(
                id="NFR-001",
                category="maintainability",
                statement=(
                    "Tests assert behaviour, not implementation detail, so a future "
                    "refactor is not blocked by them"
                ),
            ),
            NonFunctionalRequirement(
                id="NFR-002",
                category="performance",
                statement="The suite stays fast enough to run on every change",
                target="whole suite under five seconds",
            ),
        ],
        ambiguities=[
            Ambiguity(
                id="AMB-001",
                question="Is there a coverage percentage this has to reach?",
                why_it_matters=(
                    "A number turns the goal into chasing lines rather than "
                    "asserting behaviour, and the two are not the same work."
                ),
                blocking=False,
                default_assumption=(
                    "No target percentage; cover the untested modules' public "
                    "behaviour and the edge cases that can silently break"
                ),
            )
        ],
        out_of_scope=[
            "Changing production code, including the known expiry defect",
            "Load or performance testing",
            "A coverage measurement tool in the build",
        ],
    )


def plan() -> WorkPlan:
    return WorkPlan(
        strategy=(
            "One task writes the tests, one verifies them. There is no design "
            "stage because nothing is being designed. The validation stage runs "
            "the suite and reports which modules are still unreferenced by any "
            "test, so the result is a measured position rather than a claim."
        ),
        parallelism_note="Two modules, one task; splitting them would not shorten anything.",
        tasks=[
            TaskSpec(
                id="T-010",
                title="Write direct tests for the cache and the repository",
                agent="test",
                depends_on=[],
                covers=["FR-001", "FR-002"],
                reads=["requirement", "impact_analysis"],
                writes=["test_suite_index"],
                rationale="Adds test files only; no production code is touched",
            ),
            TaskSpec(
                id="T-020",
                title="Run the suite and report remaining coverage gaps",
                agent="validation",
                depends_on=["T-010"],
                reads=["test_suite_index"],
                writes=["validation_report"],
                rationale="New tests that do not pass are not an improvement",
            ),
            TaskSpec(
                id="T-030",
                title="Summarize what is now covered and what still is not",
                agent="summary",
                depends_on=["T-020"],
                reads=["requirement"],
                writes=["run_summary"],
            ),
        ],
    )


def tests() -> CodeBundle:
    return CodeBundle(
        summary=(
            "Added 19 direct tests across the cache and the repository, covering "
            "LRU eviction, TTL expiry, alias collision, deactivation semantics and "
            "selective purge."
        ),
        files=[
            GeneratedFile(
                path="tests/test_cache.py",
                content=TEST_CACHE,
                kind=ArtifactKind.TEST,
                covers=["FR-001"],
            ),
            GeneratedFile(
                path="tests/test_repository.py",
                content=TEST_REPOSITORY,
                kind=ArtifactKind.TEST,
                covers=["FR-002"],
            ),
        ],
        notes=[
            "Eviction is asserted by reading a key to make it recently used and "
            "checking that a different one is dropped — an LRU that evicted the "
            "wrong entry would otherwise still pass.",
            "Deactivation asserts the record survives and stops resolving, because "
            "the two are different guarantees and only one is obvious.",
            "No production file is touched, so this cannot accidentally become a "
            "behaviour change.",
        ],
    )


__all__ = ["plan", "requirement", "tests"]
