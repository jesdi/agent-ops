from dispatcher import usage_providers as up
from tests.test_usage_providers import FakeUsage, session_usage, failing_replace


def test_undeletable_cache_is_not_served_to_an_independent_reader(tmp_path, monkeypatch):
    up.fetch_provider("anthropic", tmp_path, now=lambda: 1005.0,
                      adapters={"anthropic": FakeUsage(session_usage(0.10))})
    failing_replace(monkeypatch)
    cache = up.cache_path(tmp_path, "anthropic")
    real_unlink = up.Path.unlink

    def unlink(self, *a, **kw):
        if self == cache:
            raise PermissionError("read-only filesystem")
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(up.Path, "unlink", unlink)
    up.fetch_provider("anthropic", tmp_path, now=lambda: 1000.0,
                      adapters={"anthropic": FakeUsage(session_usage(0.42))})
    fake = FakeUsage(session_usage(0.55))
    u = up.fetch_provider("anthropic", tmp_path, now=lambda: 1010.0, adapters={"anthropic": fake})
    assert fake.calls == 1
    assert u.windows[0].used == 0.55


def test_read_only_state_dir_cache_is_not_served_to_an_independent_reader(tmp_path, monkeypatch):
    up.fetch_provider("anthropic", tmp_path, now=lambda: 1005.0,
                      adapters={"anthropic": FakeUsage(session_usage(0.10))})
    failing_replace(monkeypatch)
    cache = up.cache_path(tmp_path, "anthropic")
    real_unlink, real_write = up.Path.unlink, up.Path.write_text

    def unlink(self, *a, **kw):
        if self == cache:
            raise PermissionError("read-only filesystem")
        return real_unlink(self, *a, **kw)

    def write_text(self, *a, **kw):
        if self == cache:
            raise PermissionError("read-only filesystem")
        return real_write(self, *a, **kw)
    monkeypatch.setattr(up.Path, "unlink", unlink)
    monkeypatch.setattr(up.Path, "write_text", write_text)
    up.fetch_provider("anthropic", tmp_path, now=lambda: 1000.0,
                      adapters={"anthropic": FakeUsage(session_usage(0.42))})
    # the stale file is untouched; another process sees only the file and the host marker
    fake = FakeUsage(session_usage(0.55))
    u = up.fetch_provider("anthropic", tmp_path, now=lambda: 1010.0, adapters={"anthropic": fake})
    assert fake.calls == 1
    assert u.windows[0].used == 0.55


def test_marker_write_failure_still_returns_the_reading(tmp_path, monkeypatch):
    failing_replace(monkeypatch)

    def deny(self, *a, **kw):
        raise PermissionError("denied")
    monkeypatch.setattr(up.Path, "unlink", deny)
    monkeypatch.setattr(up.Path, "write_text", deny)
    monkeypatch.setattr(up.Path, "touch", deny)
    u = up.fetch_provider("anthropic", tmp_path, now=lambda: 1000.0,
                          adapters={"anthropic": FakeUsage(session_usage(0.42))})
    assert u.windows[0].used == 0.42
