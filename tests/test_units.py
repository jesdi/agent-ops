"""Content assertions on the provisioned systemd user units. The units are
plain files synced verbatim by update.sh, so these tests pin the properties
the specs mandate (cadence, failure wiring) against silent regression."""
from pathlib import Path

PROVISION = Path(__file__).resolve().parent.parent / "provision"


def unit_text(name: str) -> str:
    return (PROVISION / name).read_text()


def test_keepalive_timer_is_hourly():
    # Spec 2026-07-31-auth-resilience: daily cadence let the refresh token
    # lapse past recovery; hourly keeps it inside its refresh window.
    text = unit_text("agent-ops-keepalive.timer")
    assert "OnCalendar=hourly" in text
    assert "OnCalendar=daily" not in text
    assert "Persistent=true" in text


def test_keepalive_failure_triggers_alert_unit():
    assert ("OnFailure=agent-ops-alert@%n.service"
            in unit_text("agent-ops-keepalive.service"))


def test_alert_template_unit_runs_telegram_alert():
    text = unit_text("agent-ops-alert@.service")
    assert "telegram.alert %i" in text
    assert "op run" in text          # same secret plumbing as the dispatcher
    assert "Type=oneshot" in text


def test_sweep_timer_is_daily():
    text = unit_text("agent-ops-sweep.timer")
    assert "OnCalendar=*-*-* 03:00:00" in text
    assert "Persistent=true" in text


def test_sweep_service_runs_the_sweeper():
    text = unit_text("agent-ops-sweep.service")
    assert "Type=oneshot" in text
    assert "provision/sweep-worktrees.sh" in text
    assert "--sweep" in text
    # It reads targets.yaml and writes the event log out of the state dir.
    assert "AGENT_OPS_STATE_DIR=%h/agent-ops-state" in text


def test_sweep_service_needs_no_1password_secrets():
    """The sweeper only reads git and the already-authenticated host gh, so
    it must not carry the op plumbing — an unnecessary EnvironmentFile makes
    the unit fail on a box whose op token has lapsed."""
    text = unit_text("agent-ops-sweep.service")
    assert "op run" not in text
    assert "EnvironmentFile" not in text


def test_bootstrap_installs_and_enables_the_sweep_timer():
    text = (PROVISION / "bootstrap.sh").read_text()
    assert "provision/agent-ops-sweep.service" in text
    assert "provision/agent-ops-sweep.timer" in text
    assert "enable --now agent-ops-sweep.timer" in text


def test_keepalive_service_resolves_token_via_wrapper():
    """With CLAUDE_CODE_OAUTH_TOKEN in its env the keepalive stops competing
    for the single-use refresh token in claude-home (the rotation race that
    killed the box's login every 8-16h) and becomes a pure auth canary. The
    wrapper resolves the token from 1P per run — nothing persists on disk,
    and a box without the token degrades to today's behavior."""
    text = unit_text("agent-ops-keepalive.service")
    assert ("ExecStart=%h/agent-ops/provision/with-claude-token.sh "
            "%h/.local/bin/claude -p") in text
    assert "claude-token.env" not in text


def test_dispatcher_service_does_not_carry_the_long_lived_token():
    """The dispatcher must NOT load claude-token.env into its process env:
    sessions.py's `tmux new-session` starts the tmux server from the
    dispatcher's environment, which would expose the secret to every pane
    shell (`tmux show-environment`). The budget check reads the token file
    from the state dir instead, which also covers triage and web — the
    other fetch_usage callers — without touching their units."""
    assert "claude-token.env" not in unit_text("agent-ops-dispatcher.service")
