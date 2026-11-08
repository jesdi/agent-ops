"""Exercise the public make commands with recording tool executables."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gate(tmp_path):
    shutil.copy(ROOT / "Makefile", tmp_path)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "scripts").mkdir()
    runner = ROOT / "scripts/fast_gate.py"
    shutil.copy(runner, tmp_path / "scripts")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorder = f"""#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
tool = Path(sys.argv[0]).name
if tool == 'uv' and args[:4] == ['run', '--locked', 'python', 'scripts/fast_gate.py']:
    sys.exit(subprocess.call([sys.executable, *args[3:]]))
with open(os.environ['COMMAND_LOG'], 'a') as log:
    log.write(json.dumps([tool, args, os.environ.get('FAST_GATE_VITEST_FILES')]) + '\\n')
sys.exit(7 if os.environ.get('FAIL_COMMAND') in [' '.join([tool, *args])] else 0)
"""
    for tool in ("uv", "pnpm", "git"):
        executable = bin_dir / tool
        executable.write_text(recorder)
        executable.chmod(0o755)

    class Gate:
        def file(self, path):
            file = tmp_path / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("fixture")

        def run(self, locked="", *, tests=None, fail="", target="fast"):
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                   "COMMAND_LOG": str(tmp_path / "commands"),
                   "IMPLEMENT_SPEC_LOCKED": locked, "FAIL_COMMAND": fail}
            for variable in ("TESTS", "MAKEFLAGS", "MAKEOVERRIDES", "MFLAGS", "MAKELEVEL"):
                env.pop(variable, None)
            command = ["make", target]
            if tests is not None:
                command.append(f"TESTS={tests}")
            result = subprocess.run(command, cwd=tmp_path, env=env,
                                    capture_output=True, text=True)
            log = tmp_path / "commands"
            self.calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result

    return Gate()


def test_empty_selection_runs_checks_without_any_test_suite(gate):
    result = gate.run()
    assert result.returncode == 0, result.stderr
    assert [call[:2] for call in gate.calls] == [
        ["pnpm", ["lint"]], ["pnpm", ["typecheck"]]]
    assert "no tests selected" in result.stdout.lower()


def test_mixed_selection_routes_only_selected_tests_and_skips_fixtures(gate):
    for path in ("tests/test_alpha.py", "tests/conftest.py", "frontend/src/a.test.ts",
                 "frontend/src/test/setup.ts", "frontend/e2e/a.spec.ts", "frontend/e2e/fake-api.mjs"):
        gate.file(path)
    result = gate.run("./tests/test_alpha.py::test_one tests/conftest.py frontend/src/a.test.ts "
                      "frontend/src/test/setup.ts ./frontend/e2e/a.spec.ts frontend/e2e/fake-api.mjs")
    assert result.returncode == 0, result.stderr
    assert [call[:2] for call in gate.calls] == [
        ["pnpm", ["lint"]], ["pnpm", ["typecheck"]],
        ["uv", ["run", "--locked", "python", "-m", "pytest", "-q", "tests/test_alpha.py::test_one"]],
        ["pnpm", ["exec", "vitest", "run", "--config", "vitest.fast.config.ts"]],
        ["pnpm", ["build"]],
        ["pnpm", ["exec", "playwright", "test", "(^|/)e2e/a\\.spec\\.ts$"]],
    ]
    assert json.loads(gate.calls[3][2]) == ["src/a.test.ts"]


def test_explicit_tests_override_locked_selection(gate):
    gate.file("tests/test_chosen.py")
    result = gate.run("tests/test_missing.py", tests="tests/test_chosen.py")
    assert result.returncode == 0, result.stderr
    assert gate.calls[-1][1][-1] == "tests/test_chosen.py"


def test_explicit_empty_selection_does_not_fall_back_to_locks(gate):
    result = gate.run("tests/test_missing.py", tests="")
    assert result.returncode == 0, result.stderr
    assert len(gate.calls) == 2


def test_only_fixtures_runs_no_test_suite(gate):
    gate.file("tests/conftest.py")
    gate.file("frontend/e2e/fake-api.mjs")
    result = gate.run("tests/conftest.py frontend/e2e/fake-api.mjs")
    assert result.returncode == 0, result.stderr
    assert len(gate.calls) == 2
    assert "no tests selected" in result.stdout.lower()


@pytest.mark.parametrize("selection", ["tests/test_missing.py", "../test_escape.py", "/tmp/test_absolute.py", "tests"])
def test_invalid_or_missing_paths_fail_instead_of_running_a_suite(gate, selection):
    result = gate.run(selection)
    assert result.returncode != 0
    assert "fast gate:" in result.stderr.lower()


def test_unsupported_test_kind_is_rejected(gate):
    gate.file("frontend/src/a.test.js")
    result = gate.run("frontend/src/a.test.js")
    assert result.returncode != 0
    assert "unsupported test" in result.stderr.lower()


@pytest.mark.parametrize("override", [False, True])
def test_paths_are_literal_arguments_and_duplicates_run_once(gate, override):
    path = "tests/test_$touch-owned.py"
    gate.file(path)
    selection = f"{path} ./{path}"
    result = gate.run(tests=selection) if override else gate.run(selection)
    assert result.returncode == 0, result.stderr
    assert gate.calls[-1][1][-1] == path
    assert len(gate.calls) == 3


def test_each_python_and_browser_selection_runs_separately_to_detect_no_tests(gate):
    files = ("tests/test_a.py", "tests/test_b.py", "frontend/e2e/a.spec.ts", "frontend/e2e/b.spec.ts")
    for path in files:
        gate.file(path)
    result = gate.run(" ".join(files))
    assert result.returncode == 0, result.stderr
    assert [call[1][-1] for call in gate.calls if call[0] == "uv"] == list(files[:2])
    assert [call[1][-1] for call in gate.calls if call[1][:3] == ["exec", "playwright", "test"]] == [
        "(^|/)e2e/a\\.spec\\.ts$", "(^|/)e2e/b\\.spec\\.ts$",
    ]


@pytest.mark.parametrize("failed", [
    "pnpm lint", "pnpm typecheck",
    "uv run --locked python -m pytest -q tests/test_alpha.py",
    "pnpm exec vitest run --config vitest.fast.config.ts", "pnpm build",
    "pnpm exec playwright test (^|/)e2e/a\\.spec\\.ts$",
])
def test_any_failed_check_fails_the_fast_gate(gate, failed):
    for path in ("tests/test_alpha.py", "frontend/src/a.test.ts", "frontend/e2e/a.spec.ts"):
        gate.file(path)
    result = gate.run("tests/test_alpha.py frontend/src/a.test.ts frontend/e2e/a.spec.ts", fail=failed)
    assert result.returncode != 0
    assert " ".join([gate.calls[-1][0], *gate.calls[-1][1]]) == failed


def test_full_gate_runs_full_suites_even_with_missing_locked_paths(gate):
    result = gate.run("tests/test_missing.py", target="gate")
    assert result.returncode == 0, result.stderr
    calls = [call[:2] for call in gate.calls]
    assert ["uv", ["run", "--locked", "python", "-m", "pytest", "-q"]] in calls
    assert ["pnpm", ["test"]] in calls
    assert ["uv", ["run", "--locked", "pnpm", "gen:api"]] in calls
    assert ["pnpm", ["build"]] in calls
    assert ["uv", ["run", "--locked", "sh", ".my-skills/crap-gate/run.sh"]] in calls
