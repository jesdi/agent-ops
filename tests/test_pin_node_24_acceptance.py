"""Acceptance tests for ticket 01: Node 24 is the one supported frontend Node."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_node_version_file_is_exactly_24_newline():
    assert (ROOT / "frontend/.node-version").read_bytes() == b"24\n"


def test_engines_floor_without_package_manager():
    package = json.loads((ROOT / "frontend/package.json").read_text())
    assert package["engines"]["node"] == ">=24"
    assert "packageManager" not in package


def test_ci_pins_node_24_in_both_jobs():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    pins = [l.strip() for l in ci.splitlines() if "node-version" in l]
    assert pins == ["node-version: 24", "node-version: 24"]
    assert "node-version: 22" not in ci
