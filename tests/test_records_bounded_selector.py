"""A `records` fan-out over a GROWING series is bounded, or it is not bindable.

`alpha-engine-config-I9618`. `s3-records` reads `format: csv` but had no way
to say *how many* records and *which end*, so binding a series like
`s3://alpha-engine-research/trades/eod_pnl.csv` — 120 trading sessions today,
growing ~250 a year — would mint one permanent console entity per row.

The selector lives in `console/records_shape.py`, the ONE grammar both the
`s3-records` **adapter** and the `s3-records` **driver** read (§2.3), so this
file exercises it at all three levels: the grammar, the driver, the adapter.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from console.drivers import s3_records as driver
from console.model.descriptor import Binding
from console.records_shape import RecordsSelectorError, project, select

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)

#: Five sessions, oldest first — the order `eod_pnl.csv` is written in.
EOD_CSV = (
    "date,daily_alpha_pct,spy_return_pct\n"
    "2026-08-24,-0.11,0.30\n"
    "2026-08-25,0.04,-0.12\n"
    "2026-08-26,-0.22,0.41\n"
    "2026-08-27,0.15,0.02\n"
    "2026-08-28,-0.08,0.19\n"
)


def _rows(n: int) -> list[dict]:
    return [{"i": i} for i in range(n)]


# --------------------------------------------------------------------------
# The grammar.
# --------------------------------------------------------------------------

def test_no_limit_is_the_unbounded_default():
    rows = _rows(5)
    assert select(rows, None, None) == rows


def test_order_last_keeps_the_tail():
    assert select(_rows(5), 2, "last") == [{"i": 3}, {"i": 4}]


def test_order_first_keeps_the_head():
    assert select(_rows(5), 2, "first") == [{"i": 0}, {"i": 1}]


def test_a_limit_larger_than_the_series_is_the_whole_series():
    assert select(_rows(3), 30, "last") == _rows(3)


def test_a_limit_without_an_order_raises_rather_than_picking_an_end():
    """The rule this selector exists for.

    Which end a bounded window keeps is not inferable from the fact that it is
    bounded, and a `limit: 30` that silently took the OLDEST 30 sessions would
    publish a true number about a window nobody asked for.
    """
    with pytest.raises(RecordsSelectorError, match="requires an explicit `order`"):
        select(_rows(5), 30, None)


def test_an_order_without_a_limit_is_a_typo_not_a_no_op():
    with pytest.raises(RecordsSelectorError, match="without a `limit`"):
        select(_rows(5), None, "last")


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_limit_raises(bad):
    with pytest.raises(RecordsSelectorError, match="positive integer"):
        select(_rows(5), bad, "last")


def test_an_unknown_order_raises():
    with pytest.raises(RecordsSelectorError, match="must be 'last' or 'first'"):
        select(_rows(5), 2, "newest")


def test_a_non_integer_limit_raises():
    with pytest.raises(RecordsSelectorError, match="must be an integer"):
        select(_rows(5), "thirty", "last")


def test_project_bounds_a_csv_series_from_the_declared_end():
    records, _ = project(EOD_CSV, "csv", None, None, None, 2, "last")
    assert [r["date"] for r in records] == ["2026-08-27", "2026-08-28"]


def test_project_bounds_a_json_list_fan_out():
    body = {"sessions": [{"d": i} for i in range(10)]}
    records, root = project(body, "json", "sessions", None, None, 3, "last")
    assert [r["d"] for r in records] == [7, 8, 9]
    assert root is body


def test_project_bounds_a_grouped_fan_out():
    body = {"tiles": {"a": {"c": [{"n": 1}, {"n": 2}]},
                      "b": {"c": [{"n": 3}]}}}
    records, _ = project(body, "json", "tiles.*.c", None, "tile", 1, "last")
    assert len(records) == 1


def test_a_malformed_selector_on_a_whole_body_binding_still_raises():
    """Whole-body mode has no fan-out to bound, so a `limit` there is a
    declaration about something that does not exist. It is validated anyway —
    silently accepting a selector that selects nothing is how a binding comes
    to mean something other than what it says."""
    with pytest.raises(RecordsSelectorError):
        project({"a": 1}, "json", None, None, None, 5, None)


# --------------------------------------------------------------------------
# The driver.
# --------------------------------------------------------------------------

def _binding(**spec) -> Binding:
    return Binding(component_id="crucible-executor-book", kind="metrics",
                    driver="s3-records", spec=spec)


def _series_spec(**extra):
    return {
        "key": "s3://alpha-engine-research/trades/eod_pnl.csv",
        "format": "csv",
        "kind": "signal",
        "id_template": "portfolio-alpha:{date}",
        "fields": {"daily_alpha_pct": {"path": "daily_alpha_pct"}},
        "cadence_minutes": 1440,
        **extra,
    }


def _ctx():
    return {"document_reader": lambda key: EOD_CSV, "now": NOW}


def test_the_driver_mints_one_entity_per_row_when_unbounded():
    """The behaviour the selector exists to make optional, pinned so the
    default cannot change by accident."""
    result = driver.read(_binding(**_series_spec()), _ctx())
    assert result.ok
    assert len(result.entities) == 5


def test_the_driver_honours_a_bounded_trailing_window():
    result = driver.read(
        _binding(**_series_spec(limit=2, order="last")), _ctx(),
    )
    assert result.ok
    assert {e.id for e in result.entities} == {
        "portfolio-alpha:2026-08-27", "portfolio-alpha:2026-08-28",
    }


def test_the_driver_reports_a_bad_selector_as_a_selector_finding():
    """§3.9: when something is not on the surface, the surface says WHY.

    A malformed `limit`/`order` is a defect in the DESCRIPTOR, and must not be
    reported as a document that failed to match its declared shape — that
    sends the reader to fix the producer instead of the binding.
    """
    result = driver.read(_binding(**_series_spec(limit=30)), _ctx())
    assert not result.ok
    assert "selector" in result.unavailable
    assert "body" not in result.unavailable
    assert "explicit `order`" in result.error


# --------------------------------------------------------------------------
# The adapter reads the SAME grammar (§2.3).
# --------------------------------------------------------------------------

def test_the_adapter_reads_the_same_selector_keys():
    """One grammar, two callers: the adapter's `_project` passes the identical
    `limit`/`order` through to `records_shape.project`. Forking the selector
    into the driver alone is the §2.3 defect `records_shape` exists to avoid.
    """
    from console.adapters.s3_records import _project

    records, _ = _project(
        EOD_CSV, "csv", {"limit": 2, "order": "last"},
    )
    assert [r["date"] for r in records] == ["2026-08-27", "2026-08-28"]


def test_the_selector_is_declared_in_the_descriptor_schema():
    """A binding key the schema does not know is a key nobody can validate."""
    import json
    import pathlib

    schema = json.loads(
        (pathlib.Path(__file__).resolve().parents[1]
         / "console" / "schemas" / "component_descriptor.schema.json").read_text()
    )
    props = schema["$defs"]["binding"]["properties"]
    assert props["limit"]["type"] == "integer"
    assert props["order"]["enum"] == ["last", "first"]
    assert "default" not in props["order"], (
        "`order` must have no default — that is the whole point of it"
    )
