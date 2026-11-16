"""auth_dark_since never raises, even when the marker's metadata is unreadable."""
from pathlib import Path

from tests.test_web_sources import make_sources


def test_auth_dark_since_unstatable_marker_still_reports_dark(tmp_path, monkeypatch):
    (tmp_path / "auth-dark").write_text("not json")
    _, src = make_sources(tmp_path)
    real = Path.stat

    def deny(self, *a, **kw):
        if self.name == "auth-dark":
            raise PermissionError(self)
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", deny)
    assert src.auth_dark_since()


def test_auth_dark_since_marker_disappears_after_metadata_denied(tmp_path, monkeypatch):
    path = tmp_path / "auth-dark"
    path.write_text("not json")
    _, src = make_sources(tmp_path)
    real_stat = Path.stat

    # The dispatcher clears the incident while the console cannot stat it.
    removed = False

    def stat_during_clear(target, *args, **kwargs):
        nonlocal removed
        if target == path and not removed:
            removed = True
            path.unlink()
            raise PermissionError(target)
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_during_clear)
    assert src.auth_dark_since() is None
    assert not path.exists()


def test_auth_dark_since_metadata_recovers_without_rewriting_marker(tmp_path, monkeypatch):
    import os

    path = tmp_path / "auth-dark"
    path.write_text("not json")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    _, src = make_sources(tmp_path)
    real_stat = Path.stat
    denied = False

    def transient_denial(target, *args, **kwargs):
        nonlocal denied
        if target == path and not denied:
            denied = True
            raise PermissionError(target)
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", transient_denial)
    assert src.auth_dark_since() == "2023-11-14T22:13:20+00:00"
    assert path.read_text() == "not json"
    assert path.stat().st_mtime == 1_700_000_000


def test_auth_dark_since_preserves_empty_string_and_marker(tmp_path):
    path = tmp_path / "auth-dark"
    body = '{"since": "", "alerted": false}'
    path.write_text(body)
    original_stat = path.stat()
    _, src = make_sources(tmp_path)

    assert src.auth_dark_since() == ""
    assert path.read_text() == body
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
