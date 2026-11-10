"""Direct tests of the shared freshness verdict and its public vocabulary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from console import freshness as verdict


NOW = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)


def test_freshness_tokens() -> None:
    assert verdict.FRESH == "fresh"
    assert verdict.STALE == "stale"
    assert verdict.NO_CADENCE == "no-cadence-declared"
    assert verdict.UNREADABLE == "unreadable"
    assert verdict.NO_STAMP == "no-freshness-stamp"
    assert verdict.ABSENT == "absent"
    assert verdict.NOT_EXPECTED == "not-expected"
    assert verdict.DISABLED == "disabled"


def test_missing_stamp_precedes_missing_cadence() -> None:
    assert verdict.freshness(None, 60, 1, NOW) == "no-freshness-stamp"
    assert verdict.freshness(None, None, 1, NOW) == "no-freshness-stamp"
    assert verdict.freshness(None, None, 1, NOW, missing=verdict.ABSENT) == "absent"
    assert verdict.freshness(None, 60, 1, NOW, missing="custom-token") == "custom-token"


def test_missing_cadence_precedes_stamp_parsing() -> None:
    assert verdict.freshness(NOW.isoformat(), None, 1, NOW) == "no-cadence-declared"
    assert verdict.freshness("invalid", None, 1, NOW) == "no-cadence-declared"
    assert verdict.freshness("invalid", 60, 1, NOW) == "unreadable"


def test_utc_stamp_spellings_and_inclusive_boundary() -> None:
    stamp = NOW - timedelta(seconds=60)
    for text in (stamp.isoformat(), stamp.isoformat().replace("+00:00", "Z")):
        assert verdict.freshness(text, 60, 1, NOW) == "fresh"
        assert verdict.freshness(text, 60, 1, NOW + timedelta(seconds=1)) == "stale"


def test_staleness_factor_scales_the_limit() -> None:
    stamp = (NOW - timedelta(seconds=120)).isoformat()
    assert verdict.freshness(stamp, 60, 1, NOW) == "stale"
    assert verdict.freshness(stamp, 60, 2, NOW) == "fresh"
    assert verdict.freshness(stamp, 60, 2, NOW + timedelta(seconds=1)) == "stale"
