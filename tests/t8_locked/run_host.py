"""OWN standalone host runner; redact product stack source lines from results.

This preserves actual failure/error types and frame locations without exposing
implementation bodies to the context-isolated acceptance writer.
"""
import argparse
import json
from pathlib import Path
import sys
import time
import unittest

sys.dont_write_bytecode = True


def sanitized_exception(error):
    kind, value, traceback = error
    frames = []
    while traceback is not None:
        frame = traceback.tb_frame
        frames.append({"file": frame.f_code.co_filename, "line": traceback.tb_lineno,
                       "function": frame.f_code.co_name})
        traceback = traceback.tb_next
    return {"type": kind.__name__, "message": str(value), "frames": frames}


class Results(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = []

    def _exc_info_to_string(self, error, test):
        return json.dumps(sanitized_exception(error), indent=2)

    def addSuccess(self, test):
        self.observations.append({"test": test.id(), "result": "PASS"})
        super().addSuccess(test)

    def addFailure(self, test, error):
        entry = {"test": test.id(), "result": "FAIL", "error": sanitized_exception(error)}
        if hasattr(test, "host"):
            entry["evidence"] = test.host.evidence()
        self.observations.append(entry)
        super().addFailure(test, error)

    def addError(self, test, error):
        entry = {"test": test.id(), "result": "ERROR", "error": sanitized_exception(error)}
        if hasattr(test, "host"):
            entry["evidence"] = test.host.evidence()
        self.observations.append(entry)
        super().addError(test, error)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).parent))
    # Source import lookup is the explicitly activated worktree only.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    loader = unittest.TestLoader()
    if args.names:
        suite = loader.loadTestsFromNames(args.names)
    else:
        suite = loader.discover(str(Path(__file__).parent), pattern="test_host_acceptance.py")
    started = time.time()
    result = unittest.TextTestRunner(verbosity=1, resultclass=Results).run(suite)
    report = {"started_at": started, "finished_at": time.time(), "count": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors),
              "observations": result.observations,
              "cleanup": getattr(sys.modules.get("host_support"), "CLEANUP_AUDITS", [])}
    Path(args.report).write_text(json.dumps(report, indent=2, default=str) + "\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
