"""The index's own as-of (§5.9) — the chokepoint for CN-5.9.

The defect: `__main__.py` called `build_index` **once** and `ConsoleServer` held
that index for the process lifetime. No rebuild, no rendered build time, no way
for the surface to say its rows froze at boot.

That is §5.2's forbidden shape at the largest available grain — a claim about
the past rendered as a claim about now — and it is invisible precisely because
the rows stay internally consistent **with each other**. A console up for a week
shows a week-old fleet, confidently, and every row on it agrees.

`config.example.yaml` already declared `staleness_factor` under `console:`, and
`grep -rn staleness_factor console/` showed it was read only by two adapters and
never by the index or the renderer.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from console.index.build import BuildInfo, Supervisor
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus
from console.model.kinds import Kind, State
from console.render.html import index_freshness as html_freshness
from console.render.json import index_freshness as json_freshness

T0 = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _clock(offsets):
    """A clock returning each offset (seconds from T0) in turn, then the last."""
    seq = list(offsets)

    def tick():
        return T0 + timedelta(seconds=seq.pop(0) if len(seq) > 1 else seq[0])

    return tick


def _index(*, cadence=None, status=AdapterStatus.OK, fetched="2026-08-03T12:00:00+00:00"):
    idx = Index()
    idx.add_result(AdapterResult(
        name="checks", status=status, fetched_at=fetched,
        declared_cadence_seconds=cadence,
        entities=(Entity(kind=Kind.COMPONENT, id="c", state=State.HEALTHY,
                         provenance=Provenance("checks")),),
    ))
    return idx


# ------------------------------------------------------------ the clause ---

def test_the_landing_page_carries_an_index_build_time():
    idx = _index(cadence=3600)
    idx.build_info = BuildInfo(built_at=T0.isoformat(), adapters=idx.build_info.adapters,
                               refresh_seconds=60)
    assert "index built 2026-08-03T12:00:00+00:00" in html_freshness(idx, T0)


def test_every_page_carries_it_not_only_the_landing_view():
    """A reader who arrived on an entity page deep-linked from an alert is
    exactly the reader who must not assume what they are seeing is current."""
    from console.render.html import entity_page, landing_page, list_page

    idx = _index(cadence=3600)
    idx.build_info = BuildInfo(built_at=T0.isoformat(),
                               adapters=idx.build_info.adapters, refresh_seconds=60)
    for html in (landing_page(idx), list_page(idx, Kind.COMPONENT, {}),
                 entity_page(idx, idx.entity("c"))):
        assert "index built" in html


def test_an_index_older_than_the_shortest_cadence_renders_the_SURFACE_stale():
    """Whole-surface, not row by row — it is one fact about one index, and the
    rows below it are at most as current as it is."""
    idx = _index(cadence=60)
    idx.build_info = BuildInfo(built_at=T0.isoformat(),
                               adapters=idx.build_info.adapters, refresh_seconds=60)
    later = T0 + timedelta(seconds=61)
    assert idx.build_info.is_stale(later)
    assert "SURFACE STALE" in html_freshness(idx, later)
    assert not idx.build_info.is_stale(T0 + timedelta(seconds=59))


def test_the_shortest_cadence_wins_across_sources():
    """A surface is only as current as its most frequently-changing input. One
    fast source must not hide behind a slow one's tolerance."""
    idx = Index()
    for name, cadence in (("slow", 86400.0), ("fast", 60.0)):
        idx.add_result(AdapterResult(name=name, status=AdapterStatus.OK,
                                     fetched_at=T0.isoformat(),
                                     declared_cadence_seconds=cadence))
    assert idx.build_info.shortest_cadence_seconds == 60.0


def test_no_declared_cadence_anywhere_is_NOT_COMPUTABLE_not_fresh():
    """`principles.md` §2.7 forbids rendering "we did not measure" as "fine".
    So staleness is not asserted — and the reason is rendered instead of a
    green tick."""
    idx = _index(cadence=None)
    idx.build_info = BuildInfo(built_at=T0.isoformat(),
                               adapters=idx.build_info.adapters, refresh_seconds=60)
    far_future = T0 + timedelta(days=30)
    assert not idx.build_info.is_stale(far_future)
    assert "not computable" in idx.build_info.staleness_basis()
    assert "not computable" in html_freshness(idx, far_future)


def test_an_unstamped_index_says_it_cannot_say():
    """The honest rendering of a build with no time: not fresh, not stale —
    unable to answer, and saying so."""
    assert "build time unknown" in html_freshness(_index(), T0)


# ----------------------------------------------------- the refresh loop -----

def test_a_rebuild_swaps_atomically_and_restamps():
    builds = []

    def builder():
        idx = _index(cadence=60)
        builds.append(idx)
        return idx

    sup = Supervisor(builder, refresh_seconds=60, clock=_clock([0, 30]))
    first = sup.current
    assert first.build_info.built_at == T0.isoformat()
    assert sup.refresh_once() is True
    assert sup.current is not first
    assert sup.current.build_info.built_at == (T0 + timedelta(seconds=30)).isoformat()


def test_a_failed_refresh_keeps_the_previous_index_AND_MARKS_IT():
    """Serving stale-and-marked beats serving nothing (§2.3 — a source failure
    never empties the surface). Serving stale-and-SILENT is the failure this
    whole clause exists for."""
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("source unreachable")
        return _index(cadence=60)

    sup = Supervisor(builder, refresh_seconds=60, clock=_clock([0, 30]))
    served = sup.current
    assert sup.refresh_once() is False
    assert sup.current is served              # never emptied
    assert served.entity("c") is not None     # never truncated
    info = served.build_info
    assert info.stale_since is not None
    assert "source unreachable" in info.last_error
    assert info.is_stale(T0)                  # stale regardless of cadence
    assert "SURFACE STALE" in html_freshness(served, T0)


def test_a_collision_on_the_FIRST_build_degrades_instead_of_crashing():
    """nousergon-console-I4 / §3.6, ruled 2026-08-10: a first-boot build
    failure used to propagate out of `Supervisor.__init__` and crash the
    process before it ever served a page — the one case where a source
    failure genuinely emptied the surface (§2.3), because there was no
    previous good index for the refresh-time handling to fall back to.
    Symmetric with `test_a_failed_refresh_keeps_the_previous_index_AND_MARKS_IT`:
    the console itself must come up and say what's wrong, never just fail to
    start.
    """
    def builder():
        raise RuntimeError("namespace collision: two kinds for id 'x'")

    sup = Supervisor(builder, refresh_seconds=60, clock=_clock([0]))
    served = sup.current
    assert served is not None                # the process came up
    assert served.entity("anything") is None  # honestly empty, not fabricated
    info = served.build_info
    assert info.stale_since is not None
    assert "namespace collision" in info.last_error
    assert info.is_stale(T0)
    assert "SURFACE STALE" in html_freshness(served, T0)


def test_a_failed_refresh_is_stale_even_with_no_declared_cadence():
    """The index is older than it claims by an amount nobody has measured."""
    info = BuildInfo(built_at=T0.isoformat(), stale_since=T0.isoformat(),
                     last_error="boom")
    assert info.is_stale(T0)
    assert "last refresh failed" in info.staleness_basis()


def test_no_request_ever_observes_a_partially_built_index():
    """The swap is a single reference assignment under a lock, so a reader
    holds either the whole old graph or the whole new one — never a fleet with
    half its components, which would render as a wave of ABSENT rows."""
    barrier = threading.Event()

    def slow_builder():
        idx = Index()
        for i in range(50):
            idx.add_result(AdapterResult(
                name=f"a{i}", status=AdapterStatus.OK, fetched_at=T0.isoformat(),
                entities=(Entity(kind=Kind.COMPONENT, id=f"c{i}",
                                 state=State.HEALTHY,
                                 provenance=Provenance("t")),)))
            if i == 10:
                barrier.set()
        return idx

    sup = Supervisor(lambda: _index(cadence=60), refresh_seconds=60,
                     clock=_clock([0]))
    sizes = []

    def reader():
        barrier.wait(timeout=2)
        for _ in range(200):
            sizes.append(len(sup.current.all()))

    sup._builder = slow_builder
    t = threading.Thread(target=reader)
    t.start()
    sup.refresh_once()
    t.join()
    # Only ever the complete old index (1) or the complete new one (50).
    assert set(sizes) <= {1, 50}, sorted(set(sizes))


def test_the_refresh_cadence_is_itself_rendered():
    """A refresh interval nobody can see is a claim about freshness that
    cannot be checked."""
    sup = Supervisor(lambda: _index(cadence=3600), refresh_seconds=45,
                     clock=_clock([0]))
    assert "rebuilds every 45s" in html_freshness(sup.current, T0)


def test_the_default_refresh_is_declared_in_config():
    from console.config import DEFAULT_REFRESH_SECONDS, supervised_index

    sup = supervised_index({})
    assert sup.current.build_info.refresh_seconds == DEFAULT_REFRESH_SECONDS
    sup2 = supervised_index({"console": {"refresh_seconds": 5}})
    assert sup2.current.build_info.refresh_seconds == 5


# ------------------------------------------------------- both renderings ---

def test_freshness_is_on_every_json_payload_too(monkeypatch):
    """§3.8: a consumer that trusts a row without knowing how old the index is
    has the reader's problem, and it is worse for them because nothing prompts
    them to ask."""
    from console.render.json import payload
    from console.server.router import resolve

    idx = _index(cadence=60)
    idx.build_info = BuildInfo(built_at=T0.isoformat(),
                               adapters=idx.build_info.adapters, refresh_seconds=60)
    for route in ("/", "/component", "/component/c"):
        doc = payload(idx, resolve(route))
        assert doc["index"]["built_at"] == T0.isoformat(), route
        assert doc["index"]["refresh_seconds"] == 60, route


def test_every_source_read_is_recorded_including_the_failed_ones():
    """An adapter that failed is a fact about the surface's COMPLETENESS.
    Dropping it here would make the failure invisible on the page."""
    idx = Index()
    idx.add_result(AdapterResult(name="ok-one", status=AdapterStatus.OK,
                                 fetched_at=T0.isoformat()))
    idx.add_result(AdapterResult(name="dead", status=AdapterStatus.FAILED,
                                 fetched_at=T0.isoformat(),
                                 unavailable=("source",)))
    doc = json_freshness(idx, T0)
    assert {s["name"]: s["status"] for s in doc["sources"]} == {
        "ok-one": "ok", "dead": "failed",
    }
    assert doc["sources"][1]["unavailable"] == ["source"]


@pytest.mark.parametrize("field", ["built_at", "stale", "staleness_basis", "sources"])
def test_the_json_freshness_block_is_complete(field):
    assert field in json_freshness(_index(cadence=60), T0)
