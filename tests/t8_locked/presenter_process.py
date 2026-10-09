"""OWN subprocess driver for public presenter/process/filesystem boundaries.

The interpreter audit hook observes only open of the OWN events.jsonl. It never
replaces append_event/read_tail, their bytes, or any product function. It lets an
external current-state change occur after the audit effect's last eligibility
check and before the separate notifier eligibility check.
"""
import importlib
import json
from pathlib import Path
import sys
from dataclasses import replace
from public_errors import install

install()


def main(path):
    spec = json.loads(Path(path).read_text())
    state = importlib.import_module("dispatcher.state")
    config = importlib.import_module("dispatcher.config")
    target = config.Target(**spec["target"])
    issue = spec["issue"]
    state_dir = Path(spec["state_dir"])
    action = spec["audit_action"]
    armed = [action is not None]

    def filesystem_observation(event, args):
        if event != "open" or not armed[0] or not args:
            return
        try:
            opened = Path(args[0])
        except TypeError:
            return
        if opened != state_dir / "events.jsonl":
            return
        mode = args[1] if len(args) > 1 else None
        if mode != "a":
            return
        armed[0] = False
        task = state.load(state_dir, target.name, issue)
        if action == "park":
            state.save(state_dir, replace(task, park="fixture-known-park"))
        else:
            raise AssertionError("unknown OWN filesystem action")

    sys.addaudithook(filesystem_observation)

    class Notifier:
        def send(self, template, **ctx):
            with open(spec["calls_path"], "a") as output:
                output.write(json.dumps({"template": template, "context": ctx}) + "\n")
            return 71

    try:
        module = importlib.import_module("dispatcher.runtime_presentation")
    except ModuleNotFoundError as error:
        if error.name != "dispatcher.runtime_presentation":
            raise
        raise AssertionError("Missing behavior: supported host runtime alert presentation capability") from None
    function = getattr(module, "present_runtime_alerts", None)
    if not callable(function):
        raise AssertionError("Missing behavior: supported host runtime alert presentation capability")
    result = function(state_dir, target, issue, Notifier())
    if result is not None:
        raise AssertionError("presentation returned an unauthorized token")


if __name__ == "__main__":
    main(sys.argv[1])
