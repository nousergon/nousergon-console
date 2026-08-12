"""`object-store` DRIVER tests (`console/drivers/object_store.py`) — one
component's own declared artifact, distinct from the object-store ADAPTER's
whole-prefix scan (already covered by `test_object_store.py`).

Covers alpha-engine-config-I7050: a driver-bound artifact previously carried
`cadence_seconds` only, which `numbers.staleness_honesty()` never reads (it
reads `cadence_minutes` specifically) — so no component descriptor's own
`artifacts:` binding was ever auditable by §9.6, regardless of how it was
configured.
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.drivers import object_store as driver
from console.model.descriptor import Binding

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


def _binding(spec):
    return Binding(component_id="comp-x", kind="artifact", driver="object-store", spec=spec)


def test_cadence_minutes_component_from_binding():
    def stat(uri):
        return "2026-08-12T11:00:00Z"

    result = driver.read(
        _binding({"key": "s3://b/k.json", "cadence_minutes": 1440}),
        {"object_stat": stat, "now": NOW},
    )
    assert result.entities[0].detail["cadence_minutes"] == 1440.0


def test_cadence_minutes_derived_from_cadence_hours():
    def stat(uri):
        return "2026-08-12T11:00:00Z"

    result = driver.read(
        _binding({"key": "s3://b/k.json", "cadence_hours": 2}),
        {"object_stat": stat, "now": NOW},
    )
    assert result.entities[0].detail["cadence_seconds"] == 7200.0
    assert result.entities[0].detail["cadence_minutes"] == 120.0


def test_no_declared_cadence_means_no_cadence_minutes():
    def stat(uri):
        return "2026-08-12T11:00:00Z"

    result = driver.read(_binding({"key": "s3://b/k.json"}), {"object_stat": stat, "now": NOW})
    assert result.entities[0].detail["cadence_minutes"] is None
