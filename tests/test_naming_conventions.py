"""Conventions the layout depends on, enforced rather than described.

A naming rule that lives only in a docstring is a rule that drifts. These are
cheap, and each one exists because the alternative is a reader having to hold a
mapping in their head that nothing checks.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from asep.agents import AGENT_CLASSES, build_registry
from asep.providers.responses import SCRIPTS
from asep.scenarios import SCENARIOS

PACKAGE = Path(__file__).resolve().parents[1] / "asep"


class TestResponseModules:
    """One module per kind of work, named for the work kind."""

    def test_each_response_module_is_named_for_the_work_it_answers_for(self):
        for name, scenario in SCENARIOS.items():
            module = inspect.getmodule(SCRIPTS[name].requirement)
            assert module is not None
            basename = module.__name__.rsplit(".", 1)[-1]
            assert basename == scenario.work, (
                f"scenario '{name}' declares work='{scenario.work}' but its "
                f"responses live in {basename}.py — the two must match"
            )

    def test_every_scenario_has_a_response_module_and_vice_versa(self):
        assert set(SCENARIOS) == set(SCRIPTS), (
            "a scenario with no responses cannot run; responses with no scenario "
            "are dead weight"
        )

    def test_work_kinds_are_distinct(self):
        kinds = [s.work for s in SCENARIOS.values()]
        assert len(kinds) == len(set(kinds)), "two scenarios claiming one work kind"

    # Shared machinery rather than a response for one kind of work. Listed
    # explicitly so a genuinely orphaned module is still caught.
    HELPERS = {"__init__", "codegen"}

    def test_module_files_match_the_registry(self):
        on_disk = {
            p.stem
            for p in (PACKAGE / "providers" / "responses").glob("*.py")
            if p.stem not in self.HELPERS
        }
        declared = {s.work for s in SCENARIOS.values()}
        assert on_disk == declared, "a response module nothing routes to, or vice versa"

    def test_helpers_are_imported_by_the_responses_that_use_them(self):
        """A helper nothing imports is dead weight, not shared machinery."""
        bodies = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (PACKAGE / "providers" / "responses").glob("*.py")
        )
        for helper in self.HELPERS - {"__init__"}:
            assert f"import {helper}" in bodies, f"{helper}.py is imported by nothing"


class TestAgentModules:
    def test_every_registered_agent_class_is_reachable_by_name(self):
        registry = build_registry()
        assert {cls.name for cls in AGENT_CLASSES} == set(registry)

    def test_agent_names_are_lowercase_identifiers(self):
        """They appear in task definitions, traces and policy sets."""
        for cls in AGENT_CLASSES:
            assert cls.name.isidentifier(), cls.name
            assert cls.name.islower(), cls.name


class TestLayout:
    def test_no_module_shadows_a_dependency_the_project_imports(self):
        """`providers/responses/coverage.py` once shadowed the coverage package.

        Python 3 resolves absolute imports before local modules, so this was a
        readability hazard rather than a crash — but a file named after a tool
        the project also runs is a trap for whoever reads it next.
        """
        shadowed = {"coverage", "pytest", "openai", "pydantic", "fastapi", "json", "typing"}
        offenders = [
            p.relative_to(PACKAGE).as_posix()
            for p in PACKAGE.rglob("*.py")
            if p.stem in shadowed
        ]
        assert offenders == [], f"modules shadowing a dependency: {offenders}"

    def test_the_two_script_directories_are_not_both_called_scripts(self):
        """Top-level `scripts/` holds developer utilities; responses are responses."""
        assert not (PACKAGE / "providers" / "scripts").exists()
        assert (PACKAGE / "providers" / "responses").is_dir()

    @pytest.mark.parametrize(
        "package",
        ["agents", "models", "orchestration", "providers", "scenarios", "tools", "validation"],
    )
    def test_every_subpackage_exports_a_public_surface(self, package):
        init = PACKAGE / package / "__init__.py"
        assert init.exists(), f"{package} is not a package"
        assert "__all__" in init.read_text(encoding="utf-8"), (
            f"{package}/__init__.py should declare what it exports"
        )

    def test_module_filenames_are_snake_case(self):
        bad = [
            p.relative_to(PACKAGE).as_posix()
            for p in PACKAGE.rglob("*.py")
            if p.stem != p.stem.lower() or "-" in p.stem
        ]
        assert bad == [], f"not snake_case: {bad}"
