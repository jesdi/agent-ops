"""Migration retries must prove which version can safely retire an original."""
from dataclasses import replace

import pytest

from dispatcher import state
from dispatcher.state import Stage, TaskState

REPO = "example/solo"


def task(**changes):
    base = TaskState(target="solo", issue=5, stage=Stage.IMPLEMENT, slot=0,
                     worktree="/w", branch="b", title="original",
                     updated_at="2026-10-16T00:00:00+00:00")
    return replace(base, **changes)


def interrupted_copy(tmp_path, monkeypatch):
    state.save(tmp_path, task())
    with monkeypatch.context() as patch:
        patch.setattr(state, "_migrate_archive",
                      lambda *args: (_ for _ in ()).throw(OSError("interrupted")))
        with pytest.raises(OSError, match="interrupted"):
            state.migrate_legacy(tmp_path, {"solo": REPO})


def test_both_task_copies_changed_retains_both(tmp_path, monkeypatch):
    interrupted_copy(tmp_path, monkeypatch)
    state.save(tmp_path, task(title="legacy edit"))
    state.save(tmp_path, task(repo=REPO, title="qualified edit"))
    with pytest.warns(RuntimeWarning, match="conflicting migration copies"):
        state.migrate_legacy(tmp_path, {"solo": REPO})
    assert state.load(tmp_path, "solo", 5).title == "legacy edit"
    assert state.load(tmp_path, "solo", 5, repo=REPO).title == "qualified edit"


def test_destination_only_task_update_survives_retry(tmp_path, monkeypatch):
    interrupted_copy(tmp_path, monkeypatch)
    state.save(tmp_path, task(repo=REPO, stage=Stage.PR_OPEN))
    state.migrate_legacy(tmp_path, {"solo": REPO})
    assert state.load(tmp_path, "solo", 5) is None
    assert state.load(tmp_path, "solo", 5, repo=REPO).stage == Stage.PR_OPEN


def test_wrong_repository_replacement_keeps_original(tmp_path):
    state.save(tmp_path, task())
    state._write_task(state._path(tmp_path, "solo", 5, REPO),
                      task(repo="other/repository"))
    with pytest.warns(RuntimeWarning, match="ownership"):
        state.migrate_legacy(tmp_path, {"solo": REPO})
    assert state.load(tmp_path, "solo", 5).title == "original"
    assert state.load(tmp_path, "solo", 5, repo=REPO).repo == "other/repository"


def test_marker_only_identity_migrates_with_proven_mapping(tmp_path):
    old = tmp_path / "wake-blocked-solo-5"
    ambiguous = tmp_path / "wake-blocked-other-5"
    old.touch()
    ambiguous.touch()
    state.migrate_legacy(tmp_path, {"solo": REPO})
    assert not old.exists()
    assert (tmp_path / f"wake-blocked-{state.task_key('solo', 5, REPO)}").exists()
    assert ambiguous.exists()


def test_archive_snapshot_update_survives_interrupted_migration(tmp_path, monkeypatch):
    state.archive(tmp_path, task(stage=Stage.DONE))
    with monkeypatch.context() as patch:
        patch.setattr(state, "_verify_tree",
                      lambda *args: (_ for _ in ()).throw(OSError("interrupted")))
        with pytest.raises(OSError, match="interrupted"):
            state.migrate_legacy(tmp_path, {"solo": REPO})
    state.archive(tmp_path, task(repo=REPO, stage=Stage.CANCELED, title="new snapshot"))
    state.migrate_legacy(tmp_path, {"solo": REPO})
    archived = state.load_archived(tmp_path, "solo", 5, repo=REPO)
    assert (archived.stage, archived.title) == (Stage.CANCELED, "new snapshot")
    assert not state.archive_root(tmp_path, "solo", 5).exists()


def test_artifact_edited_on_both_sides_retains_original_tree(tmp_path, monkeypatch):
    state.archive(tmp_path, task(stage=Stage.DONE))
    old = state.archive_root(tmp_path, "solo", 5) / "content"
    old.write_text("original artifact")
    with monkeypatch.context() as patch:
        patch.setattr(state, "_verify_tree",
                      lambda *args: (_ for _ in ()).throw(OSError("interrupted")))
        with pytest.raises(OSError, match="interrupted"):
            state.migrate_legacy(tmp_path, {"solo": REPO})
    new = state.archive_root(tmp_path, "solo", 5, repo=REPO) / "content"
    old.write_text("legacy edit")
    new.write_text("qualified edit")
    with pytest.warns(RuntimeWarning, match="conflicting migration copies"):
        state.migrate_legacy(tmp_path, {"solo": REPO})
    assert old.read_text() == "legacy edit"
    assert new.read_text() == "qualified edit"
