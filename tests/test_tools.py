"""Static analysis and test execution.

These are the two places the platform gets facts instead of opinions, so they
are worth testing directly.
"""

from __future__ import annotations

from asep.tools import failing_tests, run_pytest, scan

APP = '''from fastapi import APIRouter

from .repository import UrlRepository

router = APIRouter()


class Handler:
    pass


@router.get("/api/v1/urls/{code}")
def get_url(code: str):
    return code


@router.post("/api/v1/urls", status_code=201)
def create_url():
    return {}


@router.delete("/api/v1/urls/{code}")
async def delete_url(code: str):
    return None
'''

REPO = '''class UrlRepository:
    def get(self, code):
        return None
'''

SQL = """
CREATE TABLE IF NOT EXISTS short_urls (code TEXT PRIMARY KEY);
CREATE TABLE click_events (id INTEGER PRIMARY KEY);
"""


class TestScanner:
    def _index(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "routes.py").write_text(APP, encoding="utf-8")
        (tmp_path / "app" / "repository.py").write_text(REPO, encoding="utf-8")
        (tmp_path / "schema.sql").write_text(SQL, encoding="utf-8")
        return scan(tmp_path)

    def test_routes_are_recovered_with_method_and_path(self, tmp_path):
        index = self._index(tmp_path)
        found = {(r.method, r.path) for _, r in index.routes}
        assert found == {
            ("GET", "/api/v1/urls/{code}"),
            ("POST", "/api/v1/urls"),
            ("DELETE", "/api/v1/urls/{code}"),
        }

    def test_async_handlers_are_not_missed(self, tmp_path):
        index = self._index(tmp_path)
        assert any(r.handler == "delete_url" for _, r in index.routes)

    def test_symbol_definitions_are_located(self, tmp_path):
        index = self._index(tmp_path)
        assert index.module_of("UrlRepository") == ["app/repository.py"]
        assert index.module_of("Handler") == ["app/routes.py"]

    def test_importers_are_resolved_from_the_ast(self, tmp_path):
        index = self._index(tmp_path)
        assert index.importers_of("app/repository.py") == ["app/routes.py"]

    def test_tables_are_read_out_of_sql(self, tmp_path):
        index = self._index(tmp_path)
        assert index.tables == ["click_events", "short_urls"]

    def test_a_file_that_does_not_parse_does_not_stop_the_scan(self, tmp_path):
        (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")
        (tmp_path / "fine.py").write_text("x = 1\n", encoding="utf-8")
        index = scan(tmp_path)
        assert {m.path for m in index.modules} == {"broken.py", "fine.py"}

    def test_caches_and_virtualenvs_are_ignored(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "x.py").write_text("x = 1", encoding="utf-8")
        (tmp_path / "real.py").write_text("x = 1", encoding="utf-8")
        assert [m.path for m in scan(tmp_path).modules] == ["real.py"]


class TestTestRunner:
    def test_a_passing_suite_is_reported_as_executed(self, tmp_path):
        (tmp_path / "test_x.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
        run = run_pytest(tmp_path, timeout_s=60)
        assert run.executed and run.ok
        assert run.passed == 1

    def test_failures_are_counted_and_named(self, tmp_path):
        (tmp_path / "test_x.py").write_text(
            "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 0\n", encoding="utf-8"
        )
        run = run_pytest(tmp_path, timeout_s=60)
        assert run.executed and not run.ok
        assert run.passed == 1 and run.failed == 1
        assert any("test_b" in name for name in failing_tests(run.output))

    def test_no_test_files_is_reported_rather_than_passing(self, tmp_path):
        run = run_pytest(tmp_path, timeout_s=60)
        assert run.executed is False
        assert run.ok is False
        assert "no test files" in run.reason

    def test_a_hanging_suite_is_killed_within_the_budget(self, tmp_path):
        (tmp_path / "test_slow.py").write_text(
            "import time\n\n\ndef test_a():\n    time.sleep(30)\n", encoding="utf-8"
        )
        run = run_pytest(tmp_path, timeout_s=2)
        assert run.executed is False
        assert "budget" in run.reason
