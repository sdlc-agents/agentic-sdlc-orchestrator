"""Impact analysis must be derived, not looked up.

The previous version was an eight-entry table keyed to one feature on one
codebase: correct for the shipped scenario, empty for anything else. These tests
exist to stop that coming back. The strongest of them points the analyser at a
requirement it has never seen and requires a different, sensible answer.
"""

from __future__ import annotations

from collections import Counter

import pytest

from asep.agents.codebase import classify
from asep.scenarios import SCENARIOS
from asep.tools import scan
from asep.tools.impact import analyse, missing_concepts, salient_terms

LEGACY = SCENARIOS["analytics_upgrade"].seed_from


@pytest.fixture(scope="module")
def index():
    return scan(LEGACY, include_tests=True)


def read(relative: str) -> str:
    return (LEGACY / relative).read_text(encoding="utf-8", errors="replace")


def impact_for(index, *statements):
    terms = salient_terms(*statements)
    direct, reached = analyse(index, terms, read)
    return terms, [m.path for m in direct], [m.path for m in reached]


class TestSalientTerms:
    def test_filler_words_are_dropped(self):
        terms = salient_terms("Add the new analytics to the existing service")
        assert "analytics" in terms
        for filler in ("the", "new", "existing", "service", "add"):
            assert filler not in terms

    def test_repeated_words_score_higher(self):
        terms = salient_terms("click click click", "referrer")
        assert terms["click"] > terms["referrer"]

    def test_plurals_fold_onto_the_singular(self):
        terms = salient_terms("redirects redirect")
        assert terms["redirect"] >= 2


class TestDerivationIsRealNotALookup:
    def test_a_different_requirement_produces_a_different_impact_set(self, index):
        """The decisive test: two requirements, two answers, same codebase."""
        _, analytics, _ = impact_for(
            index, "Record a click event for every redirect and report referrers"
        )
        _, caching, _ = impact_for(
            index, "Replace the in-process cache with a shared Redis cache"
        )

        assert analytics != caching, "the analyser is ignoring the requirement"
        assert "app/cache.py" in caching
        assert "app/cache.py" not in analytics

    def test_it_finds_the_module_a_requirement_names(self, index):
        _, direct, _ = impact_for(index, "Change how short codes are generated")
        assert "app/shortcode.py" in direct

    def test_an_unrelated_requirement_matches_little(self, index):
        _, direct, _ = impact_for(index, "Translate the marketing website into French")
        assert direct == [] or len(direct) < 3

    def test_every_match_carries_its_reason(self, index):
        terms = salient_terms("Record a click event for every redirect")
        direct, reached = analyse(index, terms, read)
        for match in direct + reached:
            assert match.reasons, f"{match.path} was flagged with no explanation"


class TestPropagation:
    def test_imports_of_a_changed_file_are_reached(self, index):
        _, direct, reached = impact_for(
            index, "Record a click event for every redirect and report referrers"
        )
        assert "app/api/routes.py" in direct
        # main.py imports routes.py, so it is at risk without being named.
        assert "app/main.py" in reached
        assert "app/main.py" not in direct

    def test_reached_files_are_never_also_direct(self, index):
        _, direct, reached = impact_for(index, "Record a click event for every redirect")
        assert not set(direct) & set(reached)

    def test_tests_are_excluded_from_the_impact_set(self, index):
        _, direct, reached = impact_for(index, "Record a click event for every redirect")
        for path in direct + reached:
            assert classify(path) != "tests"


class TestMissingConcepts:
    def test_it_names_what_the_codebase_has_no_home_for(self, index):
        terms = salient_terms(
            "Record a click event for every redirect",
            "Attribute clicks to a referrer",
            "Report click analytics per code",
            "Expose analytics for each click",
        )
        concepts = missing_concepts(index, terms)
        assert "analytics" in concepts
        assert "click" in concepts

    def test_it_does_not_propose_something_that_already_exists(self, index):
        terms = salient_terms("Improve the cache", "Improve the cache again")
        assert "cache" not in missing_concepts(index, terms)

    def test_a_word_mentioned_once_is_not_treated_as_a_missing_subsystem(self, index):
        terms = Counter({"quicksilver": 1})
        assert missing_concepts(index, terms) == []

    def test_singular_and_plural_do_not_both_appear(self, index):
        terms = salient_terms(
            "analytics analytics analytics", "analytics for the analytics"
        )
        concepts = missing_concepts(index, terms)
        assert not ("analytics" in concepts and "analytic" in concepts)


class TestAgentIntegration:
    def test_no_hard_coded_impact_table_remains(self):
        """The old `CONCERNS` dict must not come back."""
        import asep.agents.codebase as module

        assert not hasattr(module, "CONCERNS"), (
            "impact analysis has been re-hard-coded"
        )
