"""Listener availability is independent of cache restoration and index builds."""
import json
import threading
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from console.index import warm_start
from console.index.build import Supervisor
from console.index.graph import Index
from console.server.app import serve
from console.render import json as render_json


def test_constructor_does_no_cache_io_or_rendering(monkeypatch, tmp_path):
    def forbidden(*args):
        raise AssertionError("cache work before bind")
    monkeypatch.setattr(warm_start, "code_digest", forbidden)
    monkeypatch.setattr(warm_start, "load", forbidden)
    monkeypatch.setattr(warm_start, "_renders", forbidden)
    monkeypatch.setattr(render_json, "index_dump", forbidden)
    sup = Supervisor(Index, 180, defer_first_build=True,
                     warm_start_path=str(tmp_path / "cache"))
    assert sup.current.build_info.bootstrap


def test_home_answers_during_cache_load_and_first_build(monkeypatch):
    loading, building = threading.Event(), threading.Event()
    release_cache, release_build = threading.Event(), threading.Event()

    def load(*args):
        loading.set()
        assert release_cache.wait(10)
        return Index()

    def build():
        building.set()
        assert release_build.wait(10)
        return Index()

    monkeypatch.setattr(warm_start, "load", load)
    monkeypatch.setattr(warm_start, "save", lambda *a: True)
    sup = Supervisor(build, 180, defer_first_build=True, warm_start_path="cache")
    server = serve(sup, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sup.start()
    try:
        assert loading.wait(2)
        url = f"http://127.0.0.1:{server.server_port}/"
        for event in (release_cache, release_build):
            with urlopen(url, timeout=2) as response:
                assert response.status == 200
            with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=2) as response:
                assert json.load(response)["index"]["bootstrap"] is True
            event.set()
            if event is release_cache:
                assert building.wait(2)
    finally:
        release_cache.set()
        release_build.set()
        sup.stop()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_normal_refresh_does_not_mark_current_index_stale():
    started, release = threading.Event(), threading.Event()
    now = datetime.now(timezone.utc)

    def build():
        started.set()
        assert release.wait(5)
        return Index()

    sup = Supervisor(Index, 180, clock=lambda: now)
    sup._builder = build
    thread = threading.Thread(target=sup.refresh_once)
    thread.start()
    try:
        assert started.wait(2)
        assert not sup.current.build_info.is_stale(now)
        assert not sup.current.build_info.bootstrap
    finally:
        release.set()
        thread.join(2)
    assert not sup.current.build_info.is_stale(now)


def test_cache_failure_is_visible_and_next_refresh_recovers(monkeypatch):
    def broken_digest():
        raise OSError("cache digest unreadable")

    monkeypatch.setattr(warm_start, "code_digest", broken_digest)
    sup = Supervisor(Index, 180, defer_first_build=True, warm_start_path="cache")
    assert not sup.refresh_once()
    assert "cache digest unreadable" in sup.current.build_info.last_error
    assert not sup.current.build_info.bootstrap
    monkeypatch.setattr(warm_start, "code_digest", lambda: "test")
    monkeypatch.setattr(warm_start, "save", lambda *a: True)
    assert sup.refresh_once()
    assert sup.current.build_info.last_error is None
