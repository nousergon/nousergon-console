"""`format: day`, list `path` fallback and `overrides` on a declared field
(alpha-engine-config-I12024).

The shape that motivated them: a Crucible v2 trading session is FILED under
the day of the predictions it trades (`trading_day` 2026-11-06, a Friday) and
EXECUTES on the next session (Mon 2026-11-09). A page that labels the key as
"the last trading day" says 11-06 on 11-10. The execution date is in the
document (`calendar_date`, else the orders' own `submitted_at_utc`), never in
the key, and a session that placed no orders is held, not a trading day.
"""
from __future__ import annotations

from console.records_shape import build_fields, format_shape

NY = "America/New_York"

TRADED = {
    "trading_day": "2026-11-06",
    "calendar_date": "2026-11-09",
    "outcome": "computed",
    "summary": {"n_orders": 5, "n_filled": 5},
    "orders": [
        {"symbol": "B", "submitted_at_utc": "2026-11-09T16:36:47.432169Z"},
        {"symbol": "A", "submitted_at_utc": "2026-11-09T16:36:45.334033Z"},
    ],
}

# The session fields a page declares: when it traded, on whose predictions.
FIELDS = {
    "traded_on": {
        "path": ["calendar_date", "orders"], "format": "day", "item": "submitted_at_utc",
        "tz": NY, "date_format": "%a %m-%d", "render": "text",
        "overrides": [{"path": "summary.n_orders", "empty": True, "text": "held · no orders"}],
    },
    "predictions_of": {"path": "trading_day", "format": "day", "date_format": "%a %m-%d",
                       "render": "text"},
}


def _values(doc: dict) -> dict:
    return {k: v["value"] for k, v in build_fields(doc, FIELDS, None).items()}


def test_traded_session_shows_execution_date_and_prediction_date() -> None:
    assert _values(TRADED) == {"traded_on": "Mon 11-09", "predictions_of": "Fri 11-06"}


def test_without_calendar_date_the_earliest_order_in_new_york_wins() -> None:
    doc = {k: v for k, v in TRADED.items() if k != "calendar_date"}
    assert _values(doc)["traded_on"] == "Mon 11-09"


def test_order_stamp_after_utc_midnight_is_the_new_york_day_before() -> None:
    # 01:36Z on 11-10 is 20:36 ET on 11-09: the UTC date would be wrong.
    doc = {"trading_day": "2026-11-09", "summary": {"n_orders": 1},
           "orders": [{"submitted_at_utc": "2026-11-10T01:36:16Z"}]}
    assert _values(doc)["traded_on"] == "Mon 11-09"


def test_never_derived_from_the_key_alone() -> None:
    # Orders were sent but neither stamp is present: say nothing rather than
    # echo `trading_day` as the execution date.
    doc = {"trading_day": "2026-11-06", "summary": {"n_orders": 2}, "orders": [{}]}
    assert _values(doc)["traded_on"] == [{}]
    assert _values({"trading_day": "2026-11-06", "summary": {"n_orders": 2}})["traded_on"] is None


def test_session_with_no_orders_is_held_not_a_trading_day() -> None:
    held = {"trading_day": "2026-11-09", "calendar_date": "2026-11-10",
            "outcome": "no_orders", "summary": {"n_orders": 0}, "orders": []}
    assert _values(held)["traded_on"] == "held · no orders"


def test_run_manifest_overrides_first_match_wins() -> None:
    spec = {"ran": {"path": "calendar_date", "format": "day", "date_format": "%a %m-%d",
                    "render": "text", "overrides": [
                        {"path": "status", "equals": ["failed", "error"], "text": "failed"},
                        {"path": "outputs", "empty": True, "text": "held"}]}}

    def ran(doc: dict) -> object:
        return build_fields(doc, spec, None)["ran"]["value"]

    assert ran({"status": "ok", "calendar_date": "2026-11-10", "outputs": []}) == "held"
    assert ran({"status": "failed", "calendar_date": "2026-11-03", "outputs": []}) == "failed"
    assert ran({"status": "ok", "calendar_date": "2026-11-09",
                "outputs": [{"key": "x"}]}) == "Mon 11-09"


def test_malformed_overrides_never_match() -> None:
    spec = {"d": {"path": "d", "overrides": [{"path": "d"}, "x", {"text": "t"}]}}
    assert build_fields({"d": "v"}, spec, None)["d"]["value"] == "v"
    assert build_fields({"d": "v"}, {"d": {"overrides": "nope"}}, None)["d"]["value"] == "v"


def test_day_format_edges_render_unchanged_or_iso() -> None:
    assert format_shape("not a date", {"format": "day"}) == "not a date"
    assert format_shape("2026-11-09T16:00:00Z", {"format": "day", "tz": "Nowhere/Zone"}) \
        == "2026-11-09T16:00:00Z"
    assert format_shape("2026-11-09T16:00:00", {"format": "day"}) == "2026-11-09"
    assert format_shape("2026-11-09T23:30:00-05:00", {"format": "day"}) == "2026-11-10"
    assert format_shape([{"at": "2026-11-09"}, {"at": "2026-11-06"}], {"format": "day"}) \
        == "2026-11-06"
    assert format_shape([], {"format": "day"}) == []
    assert format_shape(20261109, {"format": "day"}) == 20261109


def test_list_path_falls_back_in_order() -> None:
    spec = {"x": {"path": ["a", "b.c"]}}
    assert build_fields({"b": {"c": 2}}, spec, None)["x"]["value"] == 2
    assert build_fields({"a": 1, "b": {"c": 2}}, spec, None)["x"]["value"] == 1
