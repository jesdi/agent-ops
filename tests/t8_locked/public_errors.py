"""OWN error reporting without reading/printing product stack source lines."""
import json
import sys


def install():
    def report(kind, value, traceback):
        frames = []
        while traceback is not None:
            frames.append({"file": traceback.tb_frame.f_code.co_filename,
                           "function": traceback.tb_frame.f_code.co_name,
                           "line": traceback.tb_lineno})
            traceback = traceback.tb_next
        sys.stderr.write(json.dumps({"type": kind.__name__, "message": str(value), "frames": frames}) + "\n")
    sys.excepthook = report
