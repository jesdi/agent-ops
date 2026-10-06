import pytest

from dispatcher.prompts import render_stage_prompt, render_triage_prompt
from dispatcher.state import Stage

CTX = dict(
    issue_number=42, issue_title="Add widget", issue_url="https://github.com/x/y/issues/42",
    repo="jesdi/portfolio_eval", branch="agent/task-42", slot=1,
    backend_port=8101, frontend_port=5201,
    verify_cmd="make e2e-slot SLOT=1", gate_cmd="make gate SLOT=1",
    spec_path="specs/2026-07-14-widget/spec.md",
    tickets_dir=".agent/tickets", ticket_count=5, pr_number=12,
    reason="check-failed", labels="auto, frontend",
    tracks="- `trivial`: Rote edits.\n- `standard`: Else.",
    ticket_tracks="A ticket may carry one line `Track: <name>`: `frontend`.",
)

STAGES = [Stage.SPEC, Stage.PLAN, Stage.IMPLEMENT, Stage.REVIEW, Stage.ADDRESS_REVIEW]


@pytest.mark.parametrize("stage", STAGES)
def test_renders_without_leftover_placeholders(stage):
    out = render_stage_prompt(stage, CTX)
    assert "$" not in out.replace("$(", "")   # only shell substitutions may keep a $
    assert "#42" in out
    assert ".agent/stage.json" in out  # every stage knows the signal protocol
    assert f'"stage": "{stage.value}"' in out


def test_spec_prompt_speaks_answers_and_done_signals():
    out = render_stage_prompt(Stage.SPEC, CTX)
    for token in ("awaiting-answers", ".agent/questionnaire.md", ".agent/prototype.html",
                  '"status": "done"', "docs: spec for #42 (agent-ops)", "auto, frontend"):
        assert token in out
    assert "awaiting-review" not in out     # the spec stage has no review gate


def test_spec_prompt_carries_the_track_list_and_signal_field():
    out = render_stage_prompt(Stage.SPEC, CTX)
    assert "- `trivial`: Rote edits." in out
    assert '"track": "<name>"' in out


def test_plan_prompt_names_the_tickets_dir_and_spec():
    out = render_stage_prompt(Stage.PLAN, CTX)
    assert CTX["spec_path"] in out and '"artifact": ".agent/tickets"' in out
    assert ".agent/questions.md" in out and "awaiting-answers" in out


def test_implement_prompt_carries_skill_tickets_gate_and_progress_note():
    out = render_stage_prompt(Stage.IMPLEMENT, CTX)
    for token in ("~/.claude/skills/implement-spec/SKILL.md",
                  "~/.codex/skills/implement-spec/SKILL.md", ".agent/tickets",
                  CTX["spec_path"], "make gate SLOT=1", "agent/task-42", "of the\n5 tickets",
                  "N/M tickets merged"):
        assert token in out
    assert '"loop"' not in out          # the skill owns its gate loop
    assert "pytest" not in out and "vitest" not in out


def test_review_prompt_carries_gates_lease_push_and_pr():
    out = render_stage_prompt(Stage.REVIEW, CTX)
    for token in ("make gate SLOT=1", "make e2e-slot SLOT=1",
                  "--force-with-lease origin agent/task-42",
                  '"loop": "review"', "awaiting-ci", "Closes #42",
                  ".agent/tickets", CTX["spec_path"],
                  "--body-file .agent/pr-body.md",
                  'git ls-files "$(dirname specs/2026-07-14-widget/spec.md)"'):
        assert token in out


def test_address_review_prompt_carries_reason_pr_and_gate():
    out = render_stage_prompt(Stage.ADDRESS_REVIEW, CTX)
    assert "#12" in out and "check-failed" in out and "make gate SLOT=1" in out
    assert "--force-with-lease origin agent/task-42" in out
    assert "awaiting-ci" in out and '"status": "done"' in out
    assert CTX["spec_path"] in out


def test_address_review_prompt_renders_for_a_task_without_a_spec_path():
    """A pr-open task from before the spec folders has no spec path."""
    out = render_stage_prompt(Stage.ADDRESS_REVIEW, {**CTX, "spec_path": ""})
    assert 'Spec path of this task: "". Read that file if it exists.' in out
    assert "an empty path and no spec" in " ".join(out.split())
    assert "$" not in out.replace("$(", "")


def test_missing_key_raises():
    with pytest.raises(KeyError):
        render_stage_prompt(Stage.SPEC, {"issue_number": 1})


def test_render_triage_prompt():
    text = render_triage_prompt({
        "repo": "o/r",
        "decisions_path": "/triage/o-r-2026-07-30.json",
        "context_json": '{"issues": []}',
        "tracks": "- `t`: w",
    })
    assert "o/r" in text and "/triage/o-r-2026-07-30.json" in text
    assert '{"issues": []}' in text and "auto" in text and "human-required" in text


def test_render_triage_prompt_missing_var_raises():
    with pytest.raises(KeyError):
        render_triage_prompt({"repo": "o/r"})


def test_triage_prompt_lists_the_tracks_and_the_label_rule():
    out = render_triage_prompt({"repo": "o/r", "decisions_path": "/triage/x.json",
                                "context_json": "{}",
                                "tracks": "- `trivial`: Rote edits.\n- `standard`: Else."})
    assert "- `trivial`: Rote edits." in out
    assert "track:<name>" in out and "exactly one" in out


def test_plan_prompt_summary_is_headings_and_lists_without_tables():
    """The console shows the summary on a phone and renders no tables."""
    out = " ".join(render_stage_prompt(Stage.PLAN, CTX).split())
    assert "headings and lists" in out and "no tables" in out


def test_plan_prompt_signals_working_before_it_applies_feedback():
    """A session that reworks the plan while its signal still says
    awaiting-review would be parked by the grace clock mid-rework."""
    out = render_stage_prompt(Stage.PLAN, CTX)
    reply = " ".join(out[out.index("## 6."):].split())
    assert '"status": "working"' in reply
    assert reply.index('"status": "working"') < reply.index("ready again")
    assert "before you change anything" in reply.lower()


def test_implement_prompt_keeps_ticket_worktrees_inside_the_task_worktree():
    out = render_stage_prompt(Stage.IMPLEMENT, CTX)
    for token in (".agent/worktrees/", "git worktree prune",
                  "ticket worktrees are removed"):
        assert token in out


def _review_para(*words):
    """The one blank-line-separated paragraph of the review prompt with every word."""
    paras = [" ".join(p.split()) for p in
             render_stage_prompt(Stage.REVIEW, CTX).split("\n\n")]
    (hit,) = [p for p in paras if all(w in p for w in words)]
    return hit


def test_review_prompt_commits_a_staged_removal_and_checks_the_push():
    """A session that died between `git rm`, the commit and the push must
    not be read as having finished the step."""
    para = _review_para("keep only spec.md", "git ls-files")
    assert "AND `git status` shows nothing staged" in para
    assert "staged and not committed is yours to commit now" in para
    assert "in every case" in para and "`git status -sb`" in para
    assert "plain push" in para
    assert para.index("commit now") < para.index("git status -sb")


def test_review_prompt_keeps_an_adr_an_earlier_session_left_uncommitted():
    para = _review_para("list `docs/adr/`")
    assert para.index("`git status`") < para.index("stays")
    assert "committed or not, stays: commit it, do not add a second" in para
    assert "term already in `CONTEXT.md`" in para


def test_review_prompt_does_not_read_implement_notes_before_its_own_review():
    look = _review_para("gh pr list")
    assert "Only check that `.agent/pr-body.md` exists" in look
    assert "do not read it before step 4" in look
    ledger = _review_para(".agent/ledger.md", "own review")
    assert "any other note of the implement session under `.agent/`" in ledger
    assert "only after" in ledger


def test_review_prompt_does_not_invent_a_missing_proposal_or_design():
    para = _review_para("not recorded")
    assert "`proposal.md`" in para and "`design.md`" in para
    assert "never invent" in para


# --- restart inputs of the spec prompt (delete with dispatcher/openspec_migration.py) ---

def _spec_items():
    """Paragraphs and single list items of the spec prompt."""
    out = render_stage_prompt(Stage.SPEC, CTX)
    return [item for para in out.split("\n\n") for item in para.split("\n- ")]


def _spec_item(*needles):
    (item,) = [i for i in _spec_items() if all(n in i for n in needles)]
    return " ".join(item.split())


def test_spec_prompt_takes_only_design_files_this_branch_added():
    item = _spec_item("-design.md", "spec-ready", "remove")
    assert ("git diff --name-only --diff-filter=A origin/main...HEAD -- docs/specs/"
            in item)
    assert "this branch added" in item
    assert "main already has" in item and "never" in item
    assert "docs/specs/*-design.md" not in item           # no wildcard over old files
    assert "-diagnosis.md" not in item


def test_spec_prompt_applies_an_operator_change_request_to_the_old_design():
    item = _spec_item("-design.md", "spec-ready", "remove")
    assert "operator message" in item and "overrides" in item
    assert "word for word" in item and "spec.md" in item


def test_spec_prompt_reads_answers_from_messages_and_the_old_review_copy():
    item = _spec_item("answered", "questionnaire.md", "settled")
    assert "operator message" in item and "the review file" in item
    assert "raise no new questionnaire" in item
    review = _spec_item("docs/review/")                  # a paragraph of its own
    assert "source of answers" in review and "questionnaire rules" in review
    # Only this task's review file: main can hold the answers of other tasks.
    assert ("git diff --name-only --diff-filter=AM origin/main...HEAD -- docs/review/"
            in review)
    assert "this branch did not add or change" in review
    assert "never a source of answers" in review


def test_spec_prompt_says_an_empty_list_of_old_design_files_is_no_work():
    item = _spec_item("-design.md", "spec-ready", "remove")
    assert "When the list is empty there is nothing to do." in item
