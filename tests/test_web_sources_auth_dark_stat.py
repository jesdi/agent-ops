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
