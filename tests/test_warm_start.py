"""A restart serves the last good index, marked stale, instead of an empty one.

Brian 2026-11-04: home was blank for ~6 minutes after every restart, because
the first build takes ~350s on the live box. `warm_start.py`.
"""
from __future__ import annotations

import os
import pickle
from datetime import datetime, timezone

from console.index import warm_start
from console.index.build import Supervisor
from console.render import json as render_json
from console.render.html import landing_page

from tests.test_home import _index

T0 = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 4, 18, 30, tzinfo=timezone.utc)


def _built(path, clock=lambda: T0):
    sup = Supervisor(_index, refresh_seconds=180, clock=clock,
                     defer_first_build=True, warm_start_path=str(path))
    assert sup.refresh_once()
    return sup


def test_a_successful_build_is_saved(tmp_path):
    path = tmp_path / "last_index.pickle"
    _built(path)
    assert path.exists()
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_a_restart_serves_the_saved_index_marked_stale(tmp_path):
    path = tmp_path / "last_index.pickle"
    _built(path)
    restarted = Supervisor(lambda: (_ for _ in ()).throw(AssertionError("not yet")),
                           refresh_seconds=180, clock=lambda: T1,
                           defer_first_build=True, warm_start_path=str(path))
    idx = restarted.current
    info = idx.build_info
    assert info.bootstrap is True
    assert info.warm_start_from == T0.isoformat()
    assert info.stale_since == T1.isoformat()
    # The probes wait on this text; a warm index must not end the wait.
    assert "has not completed yet" in info.last_error
    fresh = render_json.index_freshness(idx, now=T1)
    assert fresh["stale"] is True and fresh["sources"]
    page = landing_page(idx)
    assert "Restarted" in page and "from before the restart" in page
    assert "Trading" in page  # the rows are there, not an empty surface


def test_the_first_real_build_clears_the_warm_marker(tmp_path):
    path = tmp_path / "last_index.pickle"
    _built(path)
    restarted = Supervisor(_index, refresh_seconds=180, clock=lambda: T1,
                           defer_first_build=True, warm_start_path=str(path))
    assert restarted.refresh_once()
    info = restarted.current.build_info
    assert info.bootstrap is False and info.warm_start_from is None
    assert info.stale_since is None and info.last_error is None


def test_no_path_means_the_old_empty_bootstrap(tmp_path):
    sup = Supervisor(_index, refresh_seconds=180, clock=lambda: T0,
                     defer_first_build=True)
    info = sup.current.build_info
    assert info.bootstrap is True and not info.adapters
    assert info.warm_start_from is None


def test_a_corrupt_or_foreign_file_is_a_cold_start(tmp_path):
    path = tmp_path / "last_index.pickle"
    path.write_bytes(b"not a pickle")
    assert warm_start.load(str(path), "d") is None
    path.write_bytes(pickle.dumps({"version": 999, "digest": "d", "index": _index()}))
    assert warm_start.load(str(path), "d") is None
    sup = Supervisor(_index, refresh_seconds=180, clock=lambda: T0,
                     defer_first_build=True, warm_start_path=str(path))
    assert not sup.current.build_info.adapters


def test_after_a_code_change_it_must_still_render(tmp_path, monkeypatch):
    path = tmp_path / "last_index.pickle"
    warm_start.save(_index(), str(path), "old-code")
    assert warm_start.load(str(path), "new-code") is not None
    monkeypatch.setattr(warm_start, "_renders", lambda index: False)
    assert warm_start.load(str(path), "new-code") is None
    # Same code: no render check needed.
    assert warm_start.load(str(path), "old-code") is not None


def test_default_path_follows_the_systemd_cache_directory(monkeypatch):
    monkeypatch.delenv(warm_start.PATH_ENV, raising=False)
    monkeypatch.delenv("CACHE_DIRECTORY", raising=False)
    assert warm_start.default_path() is None
    monkeypatch.setenv("CACHE_DIRECTORY", "/var/cache/nousergon-console")
    assert warm_start.default_path() == "/var/cache/nousergon-console/last_index.pickle"
    monkeypatch.setenv(warm_start.PATH_ENV, "/x/y.pickle")
    assert warm_start.default_path() == "/x/y.pickle"


def test_a_failed_write_never_breaks_the_build(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    sup = Supervisor(_index, refresh_seconds=180, clock=lambda: T0,
                     defer_first_build=True,
                     warm_start_path=str(blocker / "sub" / "last_index.pickle"))
    assert sup.refresh_once()
