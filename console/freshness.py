"""One freshness verdict, three readers (`console-policy.md` §5.2, §5.5).

`adapters/object_store.py` (prefix listing), `adapters/object_store.py`'s keys
mode (a HEAD per declared key) and `drivers/object_store.py` (one component's
declared key) all answer the same question about the same source shape: given a
last-modified stamp and a declared cadence, is this object fresh, stale, or
not-there. Two copies had already drifted apart on the token for "no stamp";
`shared-code-policy.md`'s second-adoption trigger fires at the third, so the
verdict lives here once.

**The three not-computable cases stay THREE facts** (§5.5): no stamp, no
declared cadence and an unparseable stamp are different findings with different
fixes, and collapsing them loses the fix.

**`missing` is the caller's, deliberately.** A prefix listing that returns a key
with no stamp has seen the object and cannot date it (`no-freshness-stamp`); a
HEAD that 404s has looked and the object is not there (`absent`). Those are not
the same finding, and the difference is a property of HOW the source was read —
which is exactly what the caller knows and this function does not.
"""
from __future__ import annotations

from datetime import datetime

FRESH = "fresh"
STALE = "stale"
NO_CADENCE = "no-cadence-declared"
UNREADABLE = "unreadable"
NO_STAMP = "no-freshness-stamp"
ABSENT = "absent"


def freshness(
    last_modified: str | None,
    cadence_seconds: float | None,
    staleness_factor: float,
    now: datetime,
    *,
    missing: str = NO_STAMP,
) -> str:
    """§5.1's second half: an Artifact carries the value itself, never one of
    the thirteen component states. Forcing "this object has no cadence
    declared" into a component vocabulary is precisely the pressure that
    produced the `UNKNOWN` fall-through `observability-policy.md` §8.3 forbids
    by name."""
    if last_modified is None:
        return missing
    if cadence_seconds is None:
        return NO_CADENCE
    try:
        ts = datetime.fromisoformat(str(last_modified).replace("Z", "+00:00"))
    except ValueError:
        return UNREADABLE
    return FRESH if (now - ts).total_seconds() <= cadence_seconds * staleness_factor else STALE
