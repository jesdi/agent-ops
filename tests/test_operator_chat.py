"""Operator chat: Telegram commands, replies, /status, /queue, /boost and /next."""
from dataclasses import replace as dc_replace

import dispatcher.main as main
import telegram.inbound as inbound
from telegram.inbound import Command, Plain, Reply
from dispatcher import triage
from dispatcher import operator_chat
from dispatcher.board import BoardError
from dispatcher.state import (NO_SLOT, Stage, TaskState, load, save, SessionRecord, write_session)
from tests.configfakes import with_project
from tests.dispatchfakes import (FakeGitHub, FakeSessions,
                                 cfg, deps, make_task, patch_events, patch_usage,
                                 patch_workspace, replace_capacity, row)
from tests.statuses import human, review, login, ci
from dispatcher.status import Kind


def test_reply_wakes_matching_parked_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)  # active task occupies the only slot
    patch_events(monkeypatch, [Reply(reply_to_msg_id=55, text="use oauth")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "use oauth"


def test_reply_to_unknown_message_is_reported(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    patch_events(monkeypatch, [Reply(reply_to_msg_id=999, text="hi")])
    d = deps()
    main.run_pass(c, d)
    assert "status" in d.notifier.sent  # "(reply didn't match any parked task)"


def test_plain_text_with_single_parked_task_wakes_it(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)
    patch_events(monkeypatch, [Plain(text="go ahead")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING


def test_plain_text_with_two_parked_tasks_asks_which(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43, status=human(), park_msg_id=56)
    patch_events(monkeypatch, [Plain(text="yes")])
    d = deps()
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_HUMAN
    assert load(c.state_dir, "portfolio_eval", 43).status.kind is Kind.PARKED_HUMAN
    assert "status" in d.notifier.sent  # the "Which task?" prompt


def test_attach_command_sets_hold(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)
    patch_events(monkeypatch, [Command(name="attach", issue=42)])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING and t.hold_for_attach is True


def test_attach_with_an_ambiguous_issue_number_wakes_nothing(tmp_path, monkeypatch):
    from tests.test_mpb_t03_messages_and_markers import (make_task_for,
                                                          two_repository_cfg)
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_repository_cfg(tmp_path)
    make_task_for(c, "portfolio_eval", 7, status=human(), park_msg_id=55)
    make_task_for(c, "factorial", 7, status=human(), park_msg_id=56)
    patch_events(monkeypatch, [Command(name="attach", issue=7)])
    d = deps()
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 7).status.kind is Kind.PARKED_HUMAN
    assert load(c.state_dir, "factorial", 7).status.kind is Kind.PARKED_HUMAN
    lines = [ctx["lines"] for tmpl, ctx in d.notifier.calls
             if tmpl == "status" and "ambiguous" in ctx["lines"][0]][0]
    assert sorted(lines[1:]) == ["factorial#7", "portfolio_eval#7"]


def test_attach_on_an_unparked_issue_says_so(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    patch_events(monkeypatch, [Command(name="attach", issue=42)])
    d = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d)
    assert ("status", {"lines": ["#42 is not parked"]}) in d.notifier.calls


def test_attach_command_queues_no_message(tmp_path, monkeypatch):
    """/attach is a wake, not a message. The empty text it hands _wake must
    not land in the queue: a zero-length message shows a phantom ✉ badge on
    the card, an empty row in the thread, and appends a junk "Operator
    messages" block with an empty entry to the resume prompt."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)  # the running task holds the only capacity unit
    patch_events(monkeypatch, [Command(name="attach", issue=42)])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    from dispatcher import messages
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING and t.hold_for_attach is True
    assert messages.all_messages(c.state_dir, "portfolio_eval", 42) == []


def test_status_command_reports(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=ci(7))
    patch_events(monkeypatch, [Command(name="status")])
    d = deps()
    main.run_pass(c, d)
    assert "status" in d.notifier.sent


def test_queue_command_sends_ranked_view(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(2, title="B", score=5.0, boost=1),
                          row(1, title="A", score=1.0),
                          row(3, title="C", score=None),
                          row(4, title="D", blocked=True),
                          row(5, title="E", status="In progress")])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(cfg(tmp_path), d)
    template, ctx = d.notifier.contexts[0]
    assert template == "queue"
    lines = ctx["lines"]
    assert lines[0] == "1. ↑1 [5.00] #2 B"
    assert lines[1] == "2. [1.00] #1 A"
    assert lines[2] == "3. [—] #3 C"
    assert "In progress: #5" in lines
    assert "Blocked: #4" in lines


def test_boost_command_adjusts_and_confirms(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, boost=2)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=-3)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [(7, -1)]
    assert any("#7 boost 2 → -1" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_boost_unknown_issue_replies_not_on_board(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=99, amount=1)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == []
    assert any("#99 is not on the board" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_next_eligible_sets_head_boost(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [(7, 99)]
    assert gh.statused == [] and gh.labeled == []


def test_next_ineligible_refuses_with_reason_and_hint(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="Backlog", labels=())])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == []
    joined = " ".join(l for _, ctx in d.notifier.contexts
                      for l in ctx.get("lines", []))
    assert "not eligible" in joined and "Backlog" in joined
    assert "auto" in joined and "/next 7 force" in joined


def test_next_force_flips_status_and_label_then_boosts(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="Backlog", labels=())])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.statused == [(7, "Ready")]
    assert gh.labeled == [(7, "auto")]
    assert gh.boosts == [(7, 99)]


def test_next_blocked_never_forced(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, blocked=True)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    assert any("blocked" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_boost_ambiguous_across_repositories_asks_to_disambiguate(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    other = dc_replace(c.repositories[0], name="other",
                       project=dc_replace(c.repositories[0].project, name="other"))
    c = dc_replace(c, repositories=[c.repositories[0], other])
    gh = FakeGitHub(rows=[row(7)])   # rank_rows returns #7 for BOTH projects
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=1)])
    main.run_pass(c, d)
    assert gh.boosts == []
    assert any("multiple repositories" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_gh_failure_reports_error_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    gh.boost_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=1)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_lookup_error_boost_reports_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    gh.lookup_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=1)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("boost failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_lookup_error_next_reports_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    gh.lookup_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("next failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_on_a_board_missing_a_name_reports_it(tmp_path, monkeypatch):
    patch_usage(monkeypatch)

    class NoReady(FakeGitHub):
        def set_status(self, repository, issue, status):
            raise BoardError("project jesdi/1: field 'Status' has no option 'Ready'")

    gh = NoReady(rows=[row(7, status="Backlog")])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("#7 next failed: project jesdi/1: field 'Status' has no option "
               "'Ready'" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_value_error_queue_reports_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)

    def bad_rank_rows(repositories):
        raise ValueError("malformed JSON from rank output")

    gh = FakeGitHub(rows=[row(7)])
    monkeypatch.setattr(gh, "rank_rows", bad_rank_rows)
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("/queue failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_next_in_progress_refused_without_force_hint(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="In progress")])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    joined = " ".join(l for _, ctx in d.notifier.contexts
                      for l in ctx.get("lines", []))
    assert "In progress" in joined
    assert "force" not in joined.replace("cannot be forced", "")


def test_next_in_progress_never_forced(tmp_path, monkeypatch):
    """The board is the double-dispatch guard: /next force must never flip a
    claimed issue back to Ready."""
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="In progress", labels=())])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    assert any("In progress" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_next_force_boost_failure_leaves_board_unmutated(tmp_path, monkeypatch):
    """set_boost runs first, so a Boost-field failure is a clean no-op."""
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="Backlog", labels=())])
    gh.lookup_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    assert any("#7 next failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_queue_shows_demote_marker_and_truncates(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    rows = [row(1, title="A", score=9.0, boost=-3)]
    rows += [row(n, title=f"T{n}", score=1.0) for n in range(2, 14)]
    d = deps(FakeGitHub(rows=rows))
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(cfg(tmp_path), d)
    lines = d.notifier.contexts[0][1]["lines"]
    assert lines[0] == "1. ↓3 [9.00] #1 A"
    assert len([l for l in lines if l[0].isdigit()]) == 10
    assert "… 3 more" in lines


def test_queue_labels_each_project_and_reports_empty(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    c = dc_replace(c, repositories=[c.repositories[0], with_project(
        dc_replace(c.repositories[0], name="other"), name="other")])
    d = deps(FakeGitHub(rows=[]))
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(c, d)
    lines = d.notifier.contexts[0][1]["lines"]
    assert lines == ["[portfolio_eval]", "(queue empty)",
                     "[other]", "(queue empty)"]


def test_queue_names_the_repository_in_a_project_of_several(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    first = c.repositories[0]
    c = dc_replace(c, repositories=[first, dc_replace(first, name="other")])
    d = deps(FakeGitHub(rows=[row(2, title="B", score=5.0),
                              {**row(3, title="C", score=1.0), "repository": "other"},
                              {**row(4, blocked=True), "repository": "other"}]))
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(c, d)
    assert d.notifier.contexts[0][1]["lines"] == [
        "1. [5.00] portfolio_eval#2 B", "2. [1.00] other#3 C", "Blocked: other#4"]


def test_status_counts_the_slot_the_triage_sweep_holds(tmp_path, monkeypatch):
    """The sweep's work is a tmux session, not a TaskState, so active() cannot
    see it — but _run_pass reduces effective capacity for it. Without counting
    it here, /status reports 'capacity 1/2' on a full box for the whole sweep,
    which is the operator's primary visibility surface lying."""
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, repository="portfolio_eval", stage=Stage.IMPLEMENT, slot=0,
        worktree="/wt", branch="agent/task-42", title="Fix rounding",
        updated_at="2026-07-24T00:00:00+00:00"))
    assert operator_chat._status_lines(c)[-1] == f"capacity 1/{c.capacity}"
    monkeypatch.setattr(triage, "running", lambda: True)
    lines = operator_chat._status_lines(c)
    assert lines[-1] == f"capacity 2/{c.capacity}"
    assert "triage sweep running (holds 1 slot)" in lines


def test_status_reply_content_carries_the_status_lines(tmp_path, monkeypatch):
    """Finding C: FakeNotifier previously recorded only the template name, so
    no test could assert on what a /status reply actually contains."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=ci(7))
    patch_events(monkeypatch, [Command(name="status")])
    d = deps()
    main.run_pass(c, d)
    template, ctx = [call for call in d.notifier.calls if call[0] == "status"][-1]
    assert any(line.startswith("#42 ") for line in ctx["lines"])


def test_digest_content_carries_the_status_lines(tmp_path):
    """Finding C: the digest's payload was likewise never asserted on."""
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(issue=42, repository="portfolio_eval",
                                stage=Stage.PLAN, gated=True, slot=0,
                                worktree="/x", branch="b", title="t",
                                updated_at="2026-07-14T00:00:00+00:00"))
    d = deps()
    main.send_digest(c, d)
    template, ctx = d.notifier.calls[-1]
    assert template == "daily_digest"
    assert any(line.startswith("#42 ") for line in ctx["lines"])


def test_reply_to_human_park_still_wakes(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, stage=Stage.IMPLEMENT, status=human(), park_msg_id=88)
    monkeypatch.setattr(inbound, "fetch_events",
                        lambda sd: [Reply(reply_to_msg_id=88, text="hi")])
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert sess.sent_text == []
    # _wake marks PARK_WAKE and queues the text; _drive_wakes delivers it into the prompt.
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []
    assert messages.all_messages(c.state_dir, "portfolio_eval", 42)[0].text == "hi"
    assert sess.resumed and "hi" in sess.resumed[0][1]


def test_unmatched_reply_still_reports(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, status=login(), park_msg_id=77)
    monkeypatch.setattr(inbound, "fetch_events",
                        lambda sd: [Reply(reply_to_msg_id=999, text="x")])
    d = deps()
    main.run_pass(c, d)
    assert "status" in d.notifier.sent
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.HELD_LOGIN  # untouched


def test_reply_to_the_plan_parked_message_wakes_the_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=55)
    make_task(c, issue=43)  # active task consumes the only capacity unit
    patch_events(monkeypatch, [Reply(reply_to_msg_id=55,
                                     text="drop the caching section")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "drop the caching section"


def test_plain_text_wakes_a_single_gate_parked_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=55)
    make_task(c, issue=43)
    patch_events(monkeypatch, [Plain(text="ok")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "ok"


def test_plain_text_asks_which_when_a_human_park_and_a_gate_park_coexist(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=56)
    patch_events(monkeypatch, [Plain(text="yes")])
    d = deps()
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_HUMAN
    assert load(c.state_dir, "portfolio_eval", 43).status.kind is Kind.PARKED_REVIEW
    assert "status" in d.notifier.sent  # the "Which task?" prompt lists both


def test_which_task_prompt_names_the_project_when_multi_repository(
        tmp_path, monkeypatch):
    from tests.test_multi_project_capacity import two_repository_cfg
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_repository_cfg(tmp_path)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=56)
    patch_events(monkeypatch, [Plain(text="yes")])
    d = deps()
    main.run_pass(c, d)
    prompt = "\n".join(line for t, ctx in d.notifier.calls if t == "status"
                       for line in ctx["lines"])
    assert "portfolio_eval#42" in prompt and "portfolio_eval#43" in prompt


def test_status_line_says_no_slot_for_a_gate_parked_task(tmp_path):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, repository="portfolio_eval", stage=Stage.PLAN, status=review(),
        slot=NO_SLOT, worktree="/wt", branch="agent/task-42", title="Add widget",
        updated_at="2026-07-28T00:00:00+00:00",
        effort=1, labels=("auto",)))
    lines = operator_chat._status_lines(c)
    assert lines[0].endswith("[parked-review] (no slot)")
    assert "(slot -1)" not in lines[0]
    assert lines[-1] == "capacity 0/3"   # gate-parked holds no capacity
