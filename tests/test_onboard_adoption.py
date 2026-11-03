"""An adopted head waits for the next run's rebuild, whatever the timestamps."""
from tests.test_onboard import (  # noqa: F401  (fixtures)
    box, infra_token, pushed_then_crashed, step)


def test_config_merge_waits_on_a_head_adopted_this_run(tmp_path, box, infra_token):
    sh, rec = pushed_then_crashed(tmp_path, box, "base1 tree1")
    s = step(rec, "config-merge")
    assert s["state"] == "waiting" and "next run" in s["detail"]
    assert rec["infra_pr"]["builder"] == "" and sh.ran(" push ") == []
