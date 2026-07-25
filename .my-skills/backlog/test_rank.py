import rank


def test_parse_blocked_by_basic():
    assert rank.parse_blocked_by("Blocked by: #12, #34") == [12, 34]


def test_parse_blocked_by_case_insensitive_and_multiline():
    body = "Some idea.\n\nblocked BY: #5\nMore text\nBlocked by: #5, #9"
    assert rank.parse_blocked_by(body) == [5, 9]  # deduped + sorted


def test_parse_blocked_by_none_and_empty():
    assert rank.parse_blocked_by(None) == []
    assert rank.parse_blocked_by("no blockers here") == []


def test_merge_sources_joins_fields_and_bodies():
    project_items = [
        {"content": {"number": 12, "title": "Add X", "url": "u/12"},
         "status": "Ready", "impact": 4, "effort": 2, "area": "feature"},
        {"content": {"number": 34, "title": "Do Y", "url": "u/34"}},  # untriaged, no fields
        {"title": "a draft note"},  # draft item, no content -> skipped
    ]
    issue_rows = [
        {"number": 12, "body": "Blocked by: #34", "state": "open"},
        {"number": 34, "body": "loose idea", "state": "open"},
    ]
    out = rank.merge_sources(project_items, issue_rows)
    assert [i["number"] for i in out] == [12, 34]
    twelve = out[0]
    assert twelve["state"] == "OPEN"
    assert twelve["impact"] == 4 and twelve["effort"] == 2
    assert twelve["area"] == "feature" and twelve["status"] == "Ready"
    assert twelve["body"] == "Blocked by: #34"
    thirtyfour = out[1]
    assert thirtyfour["impact"] is None and thirtyfour["status"] is None


def test_merge_sources_falls_back_to_title_join_when_content_redacted():
    # A project-scope token can list items and field values but cannot expand
    # the linked issue of a private repo: no "content", empty "repository".
    # GitHub syncs linked-item titles to issue titles, so title is the join key.
    project_items = [
        {"id": "PVTI_a", "title": "Add X", "repository": "",
         "status": "Ready", "impact": 4, "effort": 2},
    ]
    issue_rows = [
        {"number": 12, "title": "Add X", "url": "u/12",
         "body": "Blocked by: #34", "state": "open",
         "labels": [{"name": "auto"}]},
    ]
    out = rank.merge_sources(project_items, issue_rows)
    assert len(out) == 1
    twelve = out[0]
    assert twelve["number"] == 12
    assert twelve["url"] == "u/12"
    assert twelve["impact"] == 4 and twelve["status"] == "Ready"
    assert twelve["body"] == "Blocked by: #34"
    assert twelve["labels"] == ["auto"]


def test_merge_sources_redacted_item_without_matching_title_is_skipped():
    out = rank.merge_sources(
        [{"id": "PVTI_a", "title": "board-only note", "status": "Ready"}],
        [{"number": 1, "title": "Other", "url": "u/1", "body": "", "state": "open"}],
    )
    assert out == []


def test_merge_sources_title_join_skips_ambiguous_duplicate_titles():
    project_items = [{"id": "PVTI_a", "title": "Dup", "status": "Ready"}]
    issue_rows = [
        {"number": 1, "title": "Dup", "url": "u/1", "body": "", "state": "open"},
        {"number": 2, "title": "Dup", "url": "u/2", "body": "", "state": "open"},
    ]
    assert rank.merge_sources(project_items, issue_rows) == []


def test_merge_sources_missing_body_row_defaults_open_empty():
    out = rank.merge_sources(
        [{"content": {"number": 7, "title": "T", "url": "u/7"}, "impact": 3, "effort": 1}],
        [],  # no matching issue row
    )
    assert out[0]["state"] == "OPEN" and out[0]["body"] == ""


def _issue(number, body="", state="OPEN", status=None):
    return {"number": number, "title": f"i{number}", "url": "", "state": state,
            "body": body, "status": status, "impact": None, "effort": None, "area": None}


def test_availability_and_blockers():
    a = _issue(1, body="Blocked by: #2, #3, #4")
    by_number = {
        1: a,
        2: _issue(2, state="CLOSED"),          # satisfied — closed
        3: _issue(3, state="OPEN", status="Done"),  # satisfied — Status Done
        4: _issue(4, state="OPEN", status="Ready"),  # NOT satisfied — still open/active
    }
    assert rank.is_available(a, by_number) is False
    assert rank.blockers_of(a, by_number) == [4]


def test_available_when_no_blockers():
    a = _issue(1, body="just an idea")
    assert rank.is_available(a, {1: a}) is True
    assert rank.blockers_of(a, {1: a}) == []


def test_unknown_blocker_is_dropped_edge():
    a = _issue(1, body="Blocked by: #999")  # #999 not in the graph
    assert rank.is_available(a, {1: a}) is True   # dropped, not blocking
    assert rank.blockers_of(a, {1: a}) == []


def _scored(number, impact, effort, body="", state="OPEN", status="Ready"):
    return {"number": number, "title": f"i{number}", "url": "", "state": state,
            "body": body, "status": status, "impact": impact, "effort": effort, "area": None}


def test_score_ratio_and_unscored():
    assert rank.score(_scored(1, 4, 2)) == 2.0
    assert rank.score(_scored(1, None, 2)) is None
    assert rank.score(_scored(1, 4, None)) is None
    assert rank.score(_scored(1, 4, 0)) is None  # guard divide-by-zero


def test_rank_orders_available_and_separates_blocked_and_done():
    issues = [
        _scored(1, 2, 2),                       # score 1.0
        _scored(2, 5, 1),                       # score 5.0  -> rank 1
        _scored(3, None, None),                 # unscored -> last, needs triage
        _scored(4, 9, 1, body="Blocked by: #2"),  # blocked (2 is open/Ready)
        _scored(5, 4, 1, status="Done"),        # Done -> excluded entirely
    ]
    result = rank.rank_issues(issues)
    assert [i["number"] for i in result["available"]] == [2, 1, 3]
    assert [i["number"] for i in result["blocked"]] == [4]
    assert 5 not in [i["number"] for i in result["available"] + result["blocked"]]


def test_rank_excludes_closed():
    issues = [_scored(1, 3, 1, state="CLOSED"), _scored(2, 1, 1)]
    result = rank.rank_issues(issues)
    assert [i["number"] for i in result["available"]] == [2]


def test_render_contains_ranks_scores_flags_and_blocked_reason():
    issues = [
        _scored(2, 5, 1),                          # score 5.00, rank 1
        _scored(3, None, None),                    # needs triage
        _scored(4, 9, 1, body="Blocked by: #2"),   # blocked on #2
    ]
    text = rank.render(rank.rank_issues(issues))
    assert "#2 i2" in text
    assert "5.00" in text
    assert "[needs triage]" in text
    assert "Blocked" in text and "waiting on #2" in text


def test_render_no_blocked_section_when_none_blocked():
    text = rank.render(rank.rank_issues([_scored(1, 2, 1)]))
    assert "Blocked" not in text


def test_rank_separates_in_progress_claimed():
    issues = [
        _scored(1, 2, 2),                            # score 1.0, available
        _scored(2, 5, 1, status="In progress"),      # claimed -> never recommended
    ]
    result = rank.rank_issues(issues)
    assert [i["number"] for i in result["available"]] == [1]
    assert [i["number"] for i in result["in_progress"]] == [2]
    assert result["blocked"] == []


def test_in_progress_with_blockers_goes_to_claimed_not_blocked():
    issues = [
        _scored(1, 3, 1),
        _scored(2, 4, 1, status="In progress", body="Blocked by: #1"),
    ]
    result = rank.rank_issues(issues)
    assert [i["number"] for i in result["in_progress"]] == [2]
    assert result["blocked"] == []


def test_in_progress_item_still_blocks_its_dependents():
    # A claimed blocker is not yet Done -> dependents stay blocked.
    issues = [
        _scored(1, 3, 1, status="In progress"),
        _scored(2, 4, 1, body="Blocked by: #1"),
    ]
    result = rank.rank_issues(issues)
    assert [i["number"] for i in result["blocked"]] == [2]
    assert [i["number"] for i in result["in_progress"]] == [1]


def test_render_shows_claimed_section_first():
    issues = [_scored(1, 2, 1), _scored(2, 5, 1, status="In progress")]
    text = rank.render(rank.rank_issues(issues))
    assert text.startswith("In progress (claimed):")
    assert "#2 i2" in text


def test_render_no_claimed_section_when_none_in_progress():
    text = rank.render(rank.rank_issues([_scored(1, 2, 1)]))
    assert "claimed" not in text


import os

import pytest


def test_find_project_meta_in_cwd(tmp_path, monkeypatch):
    backlog = tmp_path / ".backlog"
    backlog.mkdir()
    meta_file = backlog / "project-meta.json"
    meta_file.write_text("{}")
    monkeypatch.chdir(tmp_path)
    assert rank.find_project_meta() == str(meta_file)


def test_find_project_meta_walks_up_from_subdir(tmp_path, monkeypatch):
    backlog = tmp_path / ".backlog"
    backlog.mkdir()
    meta_file = backlog / "project-meta.json"
    meta_file.write_text("{}")
    nested = tmp_path / "backend" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert rank.find_project_meta() == str(meta_file)


def test_find_project_meta_nearest_ancestor_wins(tmp_path, monkeypatch):
    outer = tmp_path / ".backlog"
    outer.mkdir()
    (outer / "project-meta.json").write_text("{}")
    inner_root = tmp_path / "sub"
    inner_backlog = inner_root / ".backlog"
    inner_backlog.mkdir(parents=True)
    inner_meta = inner_backlog / "project-meta.json"
    inner_meta.write_text("{}")
    monkeypatch.chdir(inner_root)
    assert rank.find_project_meta() == str(inner_meta)


def test_merge_sources_carries_label_names():
    out = rank.merge_sources(
        [{"content": {"number": 7, "title": "T", "url": "u/7"}}],
        [{"number": 7, "body": "", "state": "open",
          "labels": [{"name": "auto"}, {"name": "bug"}]}],
    )
    assert out[0]["labels"] == ["auto", "bug"]


def test_merge_sources_defaults_labels_empty():
    out = rank.merge_sources(
        [{"content": {"number": 7, "title": "T", "url": "u/7"}}], [])
    assert out[0]["labels"] == []


def test_json_rows_flags_blocked_and_keeps_rank_order():
    issues = [
        _scored(1, 2, 2),                          # score 1.0
        _scored(2, 5, 1),                          # score 5.0 -> first
        _scored(3, 9, 1, body="Blocked by: #2"),   # blocked
        _scored(4, 4, 1, status="In progress"),    # claimed
    ]
    for i in issues:
        i["labels"] = ["auto"]
    rows = rank.to_json_rows(rank.rank_issues(issues))
    assert [(r["number"], r["blocked"]) for r in rows] == [
        (2, False), (1, False), (3, True), (4, False)]
    row = rows[0]
    assert row["title"] == "i2" and row["status"] == "Ready"
    assert row["labels"] == ["auto"]
    assert "url" in row


def test_main_json_emits_machine_readable_rows(monkeypatch, capsys):
    import json as jsonlib
    monkeypatch.delenv("GH_PROJECT_TOKEN", raising=False)
    def fake_run(args, capture_output, text, check, env=None):
        class R:
            if args[1] == "project":
                stdout = jsonlib.dumps({"items": [
                    {"content": {"number": 5, "title": "Task", "url": "u/5"},
                     "status": "Ready", "impact": 4, "effort": 2}]})
            else:
                fields = args[args.index("--json") + 1]
                assert "labels" in fields
                assert "title" in fields and "url" in fields  # title-join keys
                stdout = jsonlib.dumps([
                    {"number": 5, "body": "", "state": "open",
                     "labels": [{"name": "auto"}]}])
        return R()

    monkeypatch.setattr(rank.subprocess, "run", fake_run)
    rank.main("acme", 1, "acme/private-repo", as_json=True)
    rows = jsonlib.loads(capsys.readouterr().out)
    assert rows == [{"number": 5, "title": "Task", "url": "u/5",
                     "status": "Ready", "labels": ["auto"],
                     "blocked": False, "score": 2.0, "boost": 0}]


def test_project_env_absent_without_token(monkeypatch):
    monkeypatch.delenv("GH_PROJECT_TOKEN", raising=False)
    assert rank._project_env() is None


def test_project_env_swaps_gh_token(monkeypatch):
    monkeypatch.setenv("GH_PROJECT_TOKEN", "classic-tok")
    env = rank._project_env()
    assert env["GH_TOKEN"] == "classic-tok"


def test_main_uses_project_token_only_for_project_call(monkeypatch):
    monkeypatch.setenv("GH_PROJECT_TOKEN", "classic-tok")
    calls = []

    def fake_run(args, capture_output, text, check, env=None):
        calls.append((args, env))

        class R:
            stdout = "{\"items\": []}" if args[1] == "project" else "[]"

        return R()

    monkeypatch.setattr(rank.subprocess, "run", fake_run)
    rank.main("acme", 1, "acme/private-repo")
    project_call, issue_call = calls
    assert project_call[0][1] == "project"
    assert project_call[1]["GH_TOKEN"] == "classic-tok"
    assert issue_call[0][1] == "issue"
    assert issue_call[1] is None  # stored gh auth (repo PAT) stays active


def test_find_project_meta_errors_clearly_when_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        rank.find_project_meta()
    assert ".backlog/project-meta.json" in str(exc.value)
    assert "backlog setup" in str(exc.value)


def test_merge_sources_parses_boost_and_defaults_zero():
    out = rank.merge_sources(
        [{"content": {"number": 7, "title": "T", "url": "u/7"}, "boost": 2},
         {"content": {"number": 8, "title": "U", "url": "u/8"}, "boost": ""},
         {"content": {"number": 9, "title": "V", "url": "u/9"}},
         {"content": {"number": 10, "title": "W", "url": "u/10"}, "boost": -3}],
        [])
    assert [i["boost"] for i in out] == [2, 0, 0, -3]


def test_boost_band_dominates_score():
    lo = _scored(1, 5, 1)              # score 5.0, unboosted
    hi = _scored(2, 1, 2)              # score 0.5, boosted
    hi["boost"] = 1
    result = rank.rank_issues([lo, hi])
    assert [i["number"] for i in result["available"]] == [2, 1]


def test_score_orders_within_boost_band():
    a = _scored(1, 2, 2)               # score 1.0
    b = _scored(2, 5, 1)               # score 5.0
    a["boost"] = b["boost"] = 1
    result = rank.rank_issues([a, b])
    assert [i["number"] for i in result["available"]] == [2, 1]


def test_negative_boost_sinks_below_unboosted():
    demoted = _scored(1, 9, 1)         # score 9.0 but demoted
    demoted["boost"] = -1
    plain = _scored(2, 1, 1)           # score 1.0
    result = rank.rank_issues([demoted, plain])
    assert [i["number"] for i in result["available"]] == [2, 1]


def test_unscored_boosted_enters_band_after_scored_in_band():
    scored_boosted = _scored(1, 2, 1)
    scored_boosted["boost"] = 1
    unscored_boosted = _scored(2, None, None)
    unscored_boosted["boost"] = 1
    scored_plain = _scored(3, 5, 1)
    result = rank.rank_issues([scored_plain, unscored_boosted, scored_boosted])
    assert [i["number"] for i in result["available"]] == [1, 2, 3]


def test_json_rows_include_boost():
    a = _scored(1, 2, 1)
    a["boost"] = 2
    a["labels"] = ["auto"]
    b = _scored(2, 5, 1)
    b["labels"] = ["auto"]
    rows = rank.to_json_rows(rank.rank_issues([a, b]))
    assert [(r["number"], r["boost"]) for r in rows] == [(1, 2), (2, 0)]


def test_render_marks_boost_up_and_down():
    up = _scored(1, 2, 1)
    up["boost"] = 2
    down = _scored(2, 5, 1)
    down["boost"] = -1
    plain = _scored(3, 1, 1)
    text = rank.render(rank.rank_issues([up, down, plain]))
    line_for_up = next(l for l in text.splitlines() if "#1 i1" in l)
    assert "↑2" in line_for_up
    line_for_down = next(l for l in text.splitlines() if "#2 i2" in l)
    assert "↓1" in line_for_down
    line_for_plain = next(l for l in text.splitlines() if "#3 i3" in l)
    assert "↑" not in line_for_plain and "↓" not in line_for_plain
