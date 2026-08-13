"""The index has an as-of too, and it bounds every other one (§5.9).

A surface built once at start and served indefinitely renders every row's
as-of **frozen at boot** while looking exactly like a live one — and it is
invisible precisely because the rows stay internally consistent with each
other. That is §5.2's forbidden shape at the largest available grain: a claim
about the past rendered as a claim about now, with nothing on the page
contradicting it. A console up for a week shows a week-old fleet, confidently.

So the index carries a `BuildInfo`: when it was built, which adapters were read
and when, which failed, and the shortest cadence any of its sources declared.
From those, one derived fact — **is this whole surface stale** — rendered at
surface level rather than row by row, because it is one fact about one index.

The refresh loop lives here too. Three properties, and each exists because its
absence is a specific way of lying:

- **Atomic swap.** A request never sees a half-built index, so a rebuild cannot
  briefly render a fleet that has half its components.
- **A failed refresh keeps serving the previous index, WITH A MARKER.** Serving
  stale-and-marked beats serving nothing (§2.3 — a source failure never empties
  the surface); serving stale-and-silent is the failure this module exists for.
- **The cadence is itself rendered.** A refresh interval nobody can see is a
  claim about freshness that cannot be checked.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable

from ..model.envelope import AdapterResult, AdapterStatus


@dataclass(frozen=True)
class AdapterFetch:
    """One source's read: when, whether it worked, and what it could not give."""

    name: str
    status: str
    fetched_at: str | None
    cadence_seconds: float | None
    unavailable: tuple[str, ...] = ()

    @classmethod
    def of(cls, result: AdapterResult) -> "AdapterFetch":
        return cls(
            name=result.name,
            status=result.status.value,
            fetched_at=result.fetched_at,
            cadence_seconds=result.declared_cadence_seconds,
            unavailable=result.unavailable,
        )


@dataclass(frozen=True)
class BuildInfo:
    """When this index was built and from what. Rendered, never inferred."""

    built_at: str
    adapters: tuple[AdapterFetch, ...] = ()
    #: How often the console intends to rebuild. Declared so a reader can check
    #: the freshness claim rather than take it.
    refresh_seconds: float | None = None
    #: Set when a rebuild was attempted and failed: this index is the PREVIOUS
    #: one, still served, and saying so. Never silently retained.
    stale_since: str | None = None
    last_error: str | None = None
    #: How long the pass that produced this index actually took.
    #:
    #: Declared next to `refresh_seconds` on purpose: the two together are the
    #: only way a reader can tell whether the cadence is a promise the builder
    #: can keep. Measured 2026-08-12 on the live box, a full pass was 93.5s
    #: against a declared 60s refresh — so the surface reported a 60-second
    #: freshness while its data was up to ~153s old, and nothing said so
    #: (alpha-engine-config-I7124).
    build_seconds: float | None = None

    @property
    def cadence_overrun(self) -> bool:
        """The build takes longer than the cadence it claims to keep.

        Not a failure — the loop does not overlap passes, so the index is
        correct. It is a HONESTY defect: `refresh_seconds` is rendered to every
        consumer as the freshness promise, and while this is true that number
        is not the interval anyone is actually getting.
        """
        if self.build_seconds is None or self.refresh_seconds is None:
            return False
        return self.build_seconds > self.refresh_seconds

    @property
    def shortest_cadence_seconds(self) -> float | None:
        """The tightest freshness promise any source made.

        The index may not outlive it: a surface is only as current as its most
        frequently-changing input, and reporting otherwise would let one
        fast-moving source hide behind a slow one's tolerance.
        """
        declared = [
            a.cadence_seconds for a in self.adapters
            if a.cadence_seconds and a.cadence_seconds > 0
        ]
        return min(declared) if declared else None

    def age_seconds(self, now: datetime) -> float | None:
        stamp = _parse(self.built_at)
        return None if stamp is None else (now - stamp).total_seconds()

    def is_stale(self, now: datetime) -> bool:
        """Whether the whole surface is stale (§5.9).

        A failed refresh is stale by definition — the index is older than it
        claims to be by an amount nobody has measured. Otherwise it is stale
        when it has outlived the shortest cadence its sources declared.

        With no declared cadence anywhere, staleness is **not computable**, and
        this returns False while `staleness_basis` says why. That is the honest
        pair: `principles.md` §2.7 forbids rendering "we did not measure" as
        "fine", so the caller renders the reason, not a green tick.
        """
        if self.stale_since is not None:
            return True
        cadence = self.shortest_cadence_seconds
        age = self.age_seconds(now)
        if cadence is None or age is None:
            return False
        return age > cadence

    def staleness_basis(self) -> str:
        if self.stale_since is not None:
            return f"last refresh failed at {self.stale_since}"
        cadence = self.shortest_cadence_seconds
        if cadence is None:
            return "not computable — no source declared a cadence"
        return f"shortest declared source cadence {cadence:g}s"


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_iso(clock: Callable[[], datetime] | None = None) -> str:
    return (clock or _utc_now)().isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Supervisor:
    """Rebuilds the index on a cadence and swaps it atomically (§5.9).

    Holds exactly one mutable reference. Handlers read `current` — never the
    index being built — so a rebuild is invisible to an in-flight request and
    a half-built graph is never rendered.
    """

    def __init__(
        self,
        builder: Callable[[], object],
        refresh_seconds: float,
        clock: Callable[[], datetime] | None = None,
        *,
        defer_first_build: bool = False,
    ) -> None:
        self._builder = builder
        self._refresh_seconds = refresh_seconds
        self._clock = clock or _utc_now
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._built_once = False
        if defer_first_build:
            # The first build is the caller's LATENCY, not just its data. On the
            # live box a full pass over six adapters takes 93.5 seconds
            # (measured 2026-08-12, 315 entities), and __main__ constructs the
            # supervisor BEFORE it binds the port — so for a minute and a half
            # after every restart, port 5180 was not listening at all. box_health
            # confirms a problem over four samples (~30s) and pages CRITICAL
            # "port not listening", which is what three crucible-dashboard
            # deploys produced in forty minutes on 2026-08-12.
            #
            # A monitoring surface that is DOWN while it works out what to say
            # is the failure it exists to detect, one layer up. So: seed the
            # same empty, marked-stale Index the build-failure path below
            # already seeds, let the caller bind immediately, and let the
            # supervisor thread do the first build. The surface comes up saying
            # "not built yet" rather than refusing connections — the identical
            # argument that comment makes about a FAILED first build, applied to
            # a SLOW one.
            self._current = self._blank_stale(
                RuntimeError("first index build has not completed yet")
            )
            return
        started = time.monotonic()
        try:
            self._current = builder()
        except Exception as exc:  # noqa: BLE001 - symmetric with refresh_once
            # A collision (or any other build failure) in the FIRST build used
            # to propagate out of __init__ and crash the process before it
            # ever bound a port — the one case where a source failure emptied
            # the surface entirely (§2.3), because there was no previous good
            # index for refresh_once()'s handling to fall back to. An empty,
            # freshly-marked-stale Index is that fallback: the monitoring
            # surface itself comes up and says what's wrong, rather than the
            # deploy just failing silently from the outside.
            # Local import: graph.py imports FROM this module (AdapterFetch,
            # BuildInfo), so a module-level import here would be circular.
            self._current = self._blank_stale(exc)
            return
        _stamp(self._current, self._clock, refresh_seconds,
               build_seconds=time.monotonic() - started)
        self._built_once = True

    def _blank_stale(self, exc: BaseException):
        """An empty index carrying the reason it is empty.

        Shared by the two cases that have no index to serve yet — the first
        build FAILED, and the first build has not FINISHED. Both want the same
        thing: a surface that comes up and says what is wrong.
        """
        # Local import: graph.py imports FROM this module (AdapterFetch,
        # BuildInfo), so a module-level import here would be circular.
        from .graph import Index

        index = Index()
        index.build_info = replace(
            index.build_info,
            built_at=now_iso(self._clock),
            refresh_seconds=self._refresh_seconds,
        )
        _mark_stale(index, self._clock, exc)
        return index

    @property
    def current(self):
        with self._lock:
            return self._current

    def refresh_once(self) -> bool:
        """Rebuild and swap. Returns whether it worked.

        A failure does NOT raise and does not clear the surface: the previous
        index stays served and is marked (§2.3, §5.9). The exception's text
        goes on the marker rather than to a log nobody reads — the person who
        needs it is looking at the page.
        """
        started = time.monotonic()
        try:
            fresh = self._builder()
        except Exception as exc:  # noqa: BLE001 - the marker IS the handling
            with self._lock:
                _mark_stale(self._current, self._clock, exc)
            return False
        _stamp(fresh, self._clock, self._refresh_seconds,
               build_seconds=time.monotonic() - started)
        with self._lock:
            self._current = fresh
            self._built_once = True
        return True

    def start(self) -> None:
        if self._thread is not None:  # pragma: no cover - idempotent guard
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:  # pragma: no cover - exercised via refresh_once
        # Build IMMEDIATELY when __init__ deferred it, rather than after the
        # first cadence wait. Waiting would leave the surface blank for
        # refresh_seconds on top of the build itself, which trades a port that
        # does not answer for a page that says nothing — a worse deal, because
        # the second one looks healthy.
        if not self._built_once:
            self.refresh_once()
        while not self._stop.wait(self._refresh_seconds):
            self.refresh_once()


def _stamp(index, clock, refresh_seconds: float,
           build_seconds: float | None = None) -> None:
    index.build_info = replace(
        index.build_info,
        built_at=now_iso(clock),
        refresh_seconds=refresh_seconds,
        build_seconds=build_seconds,
        stale_since=None,
        last_error=None,
    )


def _mark_stale(index, clock, exc: Exception) -> None:
    index.build_info = replace(
        index.build_info,
        stale_since=now_iso(clock),
        last_error=f"{type(exc).__name__}: {exc}",
    )
