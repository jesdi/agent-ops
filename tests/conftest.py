"""Ensure the checkout under test is imported, not another copy.

An editable install of agent-ops (pip install -e) registers an import
finder that resolves `dispatcher` to wherever it was installed from.
When tests run from a worktree via an entrypoint that doesn't put the
cwd on sys.path (bare `pytest`), that finder silently wins and the
wrong code gets tested. Prepending this checkout's root pins imports
to the tree the tests live in.
"""
import sys
from pathlib import Path

root = str(Path(__file__).resolve().parent.parent)
if root not in sys.path:
    sys.path.insert(0, root)
