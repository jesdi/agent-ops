"""Run candidate checks and explicitly selected tests; never infer a full suite."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_runner(path: Path) -> str | None:
    name = path.name
    if path.suffix == ".py" and (name.startswith("test_") or name.endswith("_test.py")):
        return "pytest"
    if path.is_relative_to("frontend/e2e") and name.endswith(".spec.ts"):
        return "playwright"
    if path.is_relative_to("frontend/src") and re.search(r"\.(test|spec)\.tsx?$", name):
        return "vitest"
    if re.search(r"(^test_|_test\.|\.(test|spec)\.)", name):
        raise ValueError(f"unsupported test: {path}")
    return None


def selected_tests(selections: list[str]) -> dict[str, list[str]]:
    selected: dict[str, list[str]] = {"pytest": [], "vitest": [], "playwright": []}
    for selection in selections:
        filename, separator, node = selection.partition("::")
        path = Path(filename)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"expected a repository-relative file: {selection}")
        file = ROOT / path
        if not file.is_file() or not file.resolve().is_relative_to(ROOT):
            raise ValueError(f"missing file or file outside the repository: {selection}")
        runner = test_runner(path)
        if separator and (runner != "pytest" or not node):
            raise ValueError(f"invalid pytest node ID: {selection}")
        if runner is None:
            print(f"fast gate: skipping fixture/support file {path}", flush=True)
            continue
        argument = str(path) + (separator + node if separator else "")
        if argument not in selected[runner]:
            selected[runner].append(argument)
    return selected


def run(command: list[str], *, frontend: bool = False, env=None) -> None:
    subprocess.run(command, cwd=ROOT / "frontend" if frontend else ROOT,
                   env=env, check=True)


def check(selected: dict[str, list[str]]) -> None:
    run(["pnpm", "lint"], frontend=True)
    run(["pnpm", "typecheck"], frontend=True)
    if not any(selected.values()):
        print("fast gate: no tests selected; lint and types passed", flush=True)
    for test in selected["pytest"]:
        # A valid file with zero tests must not hide behind another file's tests.
        run(["uv", "run", "--locked", "python", "-m", "pytest", "-q", test])
    if selected["vitest"]:
        files = [str(Path(path).relative_to("frontend")) for path in selected["vitest"]]
        run(["pnpm", "exec", "vitest", "run", "--config", "vitest.fast.config.ts"],
            frontend=True, env={**os.environ, "FAST_GATE_VITEST_FILES": json.dumps(files)})
    if selected["playwright"]:
        # Playwright's positional arguments are regexes, not literal file names.
        filters = ["^" + re.escape(str(ROOT / path)) + "$"
                   for path in selected["playwright"]]
        run(["pnpm", "build"], frontend=True)
        for pattern in filters:
            run(["pnpm", "exec", "playwright", "test", pattern], frontend=True)


def main() -> int:
    try:
        selections = sys.argv[1:] or os.environ.get("TESTS", os.environ.get("IMPLEMENT_SPEC_LOCKED", "")).split()
        check(selected_tests(selections))
    except subprocess.CalledProcessError as error:
        return error.returncode or 1
    except (OSError, ValueError) as error:
        print(f"fast gate: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
