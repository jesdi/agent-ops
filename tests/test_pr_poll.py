from dispatcher.pr_poll import CIStatus, PollResult, classify

SELF = "agent-bot"


def payload(state="OPEN", merged_at=None, reviews=(), comments=(),
            mergeable="MERGEABLE", head="deadbeef"):
    return {"state": state, "mergedAt": merged_at, "reviewDecision": "",
            "reviews": list(reviews), "comments": list(comments),
            "mergeable": mergeable,
            "headRefOid": head}


def check(conclusion, completed="2026-09-07T10:00:00Z", name="ci"):
    return CIStatus(conclusion, completed)


def review(ts, login, state="COMMENTED"):
    return {"submittedAt": ts, "state": state, "author": {"login": login}}


def comment(ts, login, is_bot=False):
    return {"createdAt": ts, "author": {"login": login, "is_bot": is_bot}}


def test_merged_wins_over_everything():
    p = payload(state="MERGED", merged_at="2026-07-30T10:00:00Z",
                comments=[comment("2026-07-30T09:00:00Z", "alice")])
    assert classify(p, "", SELF) == PollResult("merged")


def test_closed_unmerged():
    assert classify(payload(state="CLOSED"), "", SELF) == PollResult("closed")


def test_human_comment_with_empty_cursor_is_feedback():
    p = payload(comments=[comment("2026-07-30T09:00:00Z", "alice")])
    assert classify(p, "", SELF) == PollResult(
        "feedback", latest_ts="2026-07-30T09:00:00Z")


def test_cursor_filters_old_feedback_mixed_iso_formats():
    # Cursor is dispatcher-format (+00:00), GitHub emits Z — must compare
    # as datetimes, not strings.
    p = payload(comments=[comment("2026-07-30T09:00:00Z", "alice")])
    assert classify(p, "2026-07-30T09:30:00+00:00", SELF) == PollResult("quiet")
    assert classify(p, "2026-07-30T08:30:00+00:00", SELF).kind == "feedback"


def test_self_and_bot_authors_ignored():
    p = payload(comments=[comment("2026-07-30T09:00:00Z", SELF),
                          comment("2026-07-30T09:01:00Z", "github-actions[bot]"),
                          comment("2026-07-30T09:02:00Z", "renovate", is_bot=True)])
    assert classify(p, "", SELF) == PollResult("quiet")


def test_reviews_count_including_approvals_with_comments():
    p = payload(reviews=[review("2026-07-30T09:00:00Z", "alice", "APPROVED")])
    assert classify(p, "", SELF).kind == "feedback"


def test_latest_ts_is_max_across_reviews_and_comments():
    p = payload(reviews=[review("2026-07-30T09:00:00Z", "alice")],
                comments=[comment("2026-07-30T11:00:00Z", "bob")])
    assert classify(p, "", SELF).latest_ts == "2026-07-30T11:00:00Z"


def test_quiet_open_pr():
    assert classify(payload(), "", SELF) == PollResult("quiet")


def test_malformed_entries_ignored():
    p = payload(reviews=[{"state": "COMMENTED"}],       # no ts, no author
                comments=[{"createdAt": "", "author": None}])
    assert classify(p, "", SELF) == PollResult("quiet")


def test_red_check_is_check_failed_with_its_completion_time():
    checks = [check("success", "2026-09-07T09:00:00Z", "lint"),
              check("failure", "2026-09-07T10:00:00Z")]
    assert classify(payload(), "", SELF, ci_statuses=checks) == PollResult(
        "check-failed", "2026-09-07T10:00:00Z")


def test_red_check_does_not_retrigger_behind_the_check_cursor():
    checks = [check("failure")]
    assert classify(payload(), "", SELF, ci_statuses=checks,
                    check_cursor="2026-09-07T10:00:00Z") == PollResult("quiet")
    assert classify(payload(), "", SELF, ci_statuses=checks,
                    check_cursor="2026-09-07T09:59:00+00:00").kind == "check-failed"


def test_pending_and_timestampless_results_are_quiet():
    checks = [CIStatus("pending", "2026-09-07T10:00:00Z"), CIStatus("failure", "")]
    assert classify(payload(), "", SELF, ci_statuses=checks) == PollResult("quiet")


def test_conflict_is_reported_once_per_head():
    p = payload(mergeable="CONFLICTING", head="abc123")
    assert classify(p, "", SELF) == PollResult("conflict", "abc123")
    assert classify(p, "", SELF, conflict_cursor="abc123") == PollResult("quiet")
    assert classify(p, "", SELF, conflict_cursor="old000").kind == "conflict"


def test_feedback_outranks_red_check_and_conflict():
    p = payload(comments=[comment("2026-09-07T09:00:00Z", "alice")],
                mergeable="CONFLICTING")
    assert classify(p, "", SELF, ci_statuses=[check("failure")]).kind == "feedback"
    assert classify(p, "2026-09-07T09:30:00Z", SELF,
                    ci_statuses=[check("failure")]).kind == "check-failed"


def test_merged_outranks_red_check():
    p = payload(state="MERGED", merged_at="2026-09-07T12:00:00Z")
    assert classify(p, "", SELF, ci_statuses=[check("failure")]) == PollResult("merged")
