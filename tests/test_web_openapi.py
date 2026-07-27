import json
import subprocess
import sys


def test_openapi_export_prints_schema_with_contract_paths():
    out = subprocess.run(
        [sys.executable, "-m", "web.openapi"],
        check=True, capture_output=True, text=True,
    ).stdout
    schema = json.loads(out)
    paths = schema["paths"]
    for p in ("/api/board", "/api/queue", "/api/budget", "/api/failures",
              "/api/history", "/api/pending-intents", "/api/task/{issue}"):
        assert p in paths, f"missing {p}"
    names = schema["components"]["schemas"]
    for n in ("BoardView", "QueueView", "TaskDetail", "BudgetView",
              "FailuresView", "HistoryView"):
        assert n in names, f"missing schema {n}"
