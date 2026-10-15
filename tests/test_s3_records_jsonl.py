"""s3-records `format: jsonl` — date-partitioned JSON Lines, bounded read.

Recorded fixtures, no live bucket. The shape is a delivery stream writing one
event envelope per line under `{prefix}dt=YYYY-MM-DD/` (alpha-engine-config-
I12323's alert log). Names are synthetic (this repo is public).

What these pin:
- only the last `days` UTC partitions are listed, one listing per day;
- one entity per line, with dotted field/facet paths into the envelope;
- an empty window (the log is not live yet) is an OK source with zero rows;
- a malformed line is skipped and COUNTED, and never drops its neighbours;
- `max_objects` keeps the newest objects and says when it cut the read;
- a duplicated event (at-least-once delivery) is one row;
- malformed bounds and a fan-out declaration FAIL the source.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from console.adapters import s3_records
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind
from console.records_shape import parse_jsonl

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = "fixture-bucket"
PREFIX = "logs/events/v1/"


def _event(event_id: str, source: str, severity: str, body: str,
           occurred_at: str) -> str:
    return json.dumps({
        "version": "0", "id": event_id, "detail-type": "fixture.event.v1",
        "source": "fixture.emitter", "time": occurred_at,
        "detail": {"schema_version": 1, "origin": "alerts.publish",
                   "source": source, "severity": severity, "body": body,
                   "occurred_at": occurred_at, "dedup_key": None},
    })


DAY0 = "2026-10-15"
DAY1 = "2026-10-14"
KEY_A = f"{PREFIX}dt={DAY0}/event-log-1-2026-10-15-11-00-00-aaaa"
KEY_B = f"{PREFIX}dt={DAY1}/event-log-1-2026-10-14-09-00-00-bbbb"
BODIES = {
    KEY_A: "\n".join([
        _event("e-1", "app-a", "error", "disk full\nsecond line", "2026-10-15T10:59:30Z"),
        _event("e-2", "app-b", "info", "nightly done", "2026-10-15T10:59:50Z"),
    ]) + "\n",
    KEY_B: _event("e-3", "app-a/deploy", "warning", "deploy slow",
                  "2026-10-14T08:59:00Z") + "\n",
}
LISTING = {
    f"{PREFIX}dt={DAY0}/": [(KEY_A, "2026-10-15T11:00:05+00:00")],
    f"{PREFIX}dt={DAY1}/": [(KEY_B, "2026-10-14T09:00:05+00:00")],
}


def _cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": PREFIX,
        "format": "jsonl",
        "days": 3,
        "max_objects": 50,
        "kind": "incident",
        "question": "What did the fleet alert on, and when?",
        "key_pattern": r"dt=(?P<dt>\d{4}-\d{2}-\d{2})/event-log-",
        "id_template": "event:{id}",
        "as_of_field": "detail.occurred_at",
        "state_field": "detail.severity",
        "facets": {
            "record": {"value": "event-log"},
            "severity": {"path": "detail.severity"},
            "project": {"path": "detail.source",
                        "map": {"app-a": "project-a", "app-a/deploy": "project-a"}},
        },
        "fields": {
            "body": {"path": "detail.body", "render": "text"},
            "severity": {"path": "detail.severity", "render": "text"},
            "occurred_at": {"path": "detail.occurred_at", "render": "text"},
        },
        **extra,
    }


class _Store:
    """Records every listing prefix and every body read."""

    def __init__(self, listing=None, bodies=None):
        self.listing = LISTING if listing is None else listing
        self.bodies = BODIES if bodies is None else bodies
        self.listed: list[str] = []
        self.read: list[str] = []

    def lister(self, bucket, prefix):
        assert bucket == BUCKET
        self.listed.append(prefix)
        return list(self.listing.get(prefix, []))

    def text_reader(self, bucket, key):
        self.read.append(key)
        body = self.bodies[key]
        if isinstance(body, Exception):
            raise body
        return body


def _fetch(cfg, store):
    # `reader` is deliberately NOT passed: jsonl reads text, never the parser.
    return s3_records.fetch(cfg, lister=store.lister, text_reader=store.text_reader,
                            now=NOW)


def _by_id(result):
    return {e.id: e for e in result.entities}


def test_lists_only_the_last_n_day_partitions():
    store = _Store()
    _fetch(_cfg(days=3), store)
    assert store.listed == [f"{PREFIX}dt=2026-10-15/", f"{PREFIX}dt=2026-10-14/",
                            f"{PREFIX}dt=2026-10-13/"]
    # The bare prefix — the whole, ever-growing log — is never listed.
    assert PREFIX not in store.listed


def test_one_entity_per_line_with_dotted_paths():
    result = _fetch(_cfg(), _Store())
    assert result.status == AdapterStatus.OK
    assert result.unavailable == ()
    rows = _by_id(result)
    assert set(rows) == {"event:e-1", "event:e-2", "event:e-3"}
    e1 = rows["event:e-1"]
    assert e1.kind == Kind.INCIDENT
    assert e1.state == "error"
    assert e1.provenance.as_of == "2026-10-15T10:59:30Z"
    assert e1.provenance.evidence == f"s3://{BUCKET}/{KEY_A}"
    assert e1.facets == {"record": "event-log", "severity": "error", "project": "project-a"}
    assert e1.detail["fields"]["body"]["value"] == "disk full\nsecond line"
    assert e1.detail["key"] == KEY_A
    # Unmapped source: the `project` facet is omitted, never passed through.
    assert "project" not in rows["event:e-2"].facets
    assert rows["event:e-3"].facets["project"] == "project-a"


def test_empty_prefix_renders_empty_not_failed():
    """The log is not live yet: no partition exists. Zero rows, OK source."""
    store = _Store(listing={}, bodies={})
    result = _fetch(_cfg(days=7), store)
    assert result.status == AdapterStatus.OK
    assert result.entities == ()
    assert result.edges == ()
    assert result.unavailable == ()
    assert len(store.listed) == 7
    assert store.read == []


def test_malformed_lines_are_skipped_and_counted():
    body = "\n".join([
        _event("e-1", "app-a", "error", "kept", "2026-10-15T10:00:00Z"),
        '{"id": "torn", "detail": {"sever',     # torn line
        "[1, 2, 3]",                             # JSON, but not an object
        "",                                      # blank: not a record, not counted
        _event("e-2", "app-a", "info", "also kept", "2026-10-15T10:01:00Z"),
    ]) + "\n"
    store = _Store(listing={f"{PREFIX}dt={DAY0}/": [(KEY_A, "2026-10-15T11:00:05+00:00")]},
                   bodies={KEY_A: body})
    result = _fetch(_cfg(), store)
    assert result.status == AdapterStatus.OK
    assert set(_by_id(result)) == {"event:e-1", "event:e-2"}
    assert result.unavailable == ("malformed_lines:2",)


def test_malformed_count_sums_across_objects():
    bodies = {KEY_A: "not json\n" + BODIES[KEY_A], KEY_B: BODIES[KEY_B] + "{oops\n"}
    result = _fetch(_cfg(), _Store(bodies=bodies))
    assert len(result.entities) == 3
    assert "malformed_lines:2" in result.unavailable


def test_max_objects_keeps_the_newest_and_says_it_cut():
    keys = [f"{PREFIX}dt={DAY0}/event-log-1-2026-10-15-0{h}-00-00-x" for h in range(5)]
    listing = {f"{PREFIX}dt={DAY0}/": [(k, f"2026-10-15T0{h}:00:05+00:00")
                                       for h, k in enumerate(keys)]}
    bodies = {k: _event(f"e-{h}", "app-a", "info", "x", f"2026-10-15T0{h}:00:00Z")
              for h, k in enumerate(keys)}
    store = _Store(listing=listing, bodies=bodies)
    result = _fetch(_cfg(max_objects=2), store)
    assert store.read == [keys[4], keys[3]]
    assert set(_by_id(result)) == {"event:e-4", "event:e-3"}
    assert result.unavailable == ("max_objects",)


def test_max_objects_counts_only_matching_keys():
    listing = {f"{PREFIX}dt={DAY0}/": [(KEY_A, "2026-10-15T11:00:05+00:00"),
                                       (f"{PREFIX}dt={DAY0}/unrelated.json",
                                        "2026-10-15T11:30:00+00:00")]}
    store = _Store(listing=listing)
    result = _fetch(_cfg(max_objects=1), store)
    assert store.read == [KEY_A]
    assert "max_objects" not in result.unavailable


def test_a_duplicated_event_is_one_row():
    dup_key = f"{PREFIX}dt={DAY0}/event-log-1-2026-10-15-11-05-00-cccc"
    listing = {f"{PREFIX}dt={DAY0}/": [(KEY_A, "2026-10-15T11:00:05+00:00"),
                                       (dup_key, "2026-10-15T11:05:05+00:00")]}
    bodies = {KEY_A: BODIES[KEY_A], dup_key: BODIES[KEY_A].splitlines()[0] + "\n"}
    result = _fetch(_cfg(), _Store(listing=listing, bodies=bodies))
    assert sorted(e.id for e in result.entities) == ["event:e-1", "event:e-2"]


def test_unreadable_object_is_partial_not_fatal():
    bodies = {KEY_A: OSError("gone"), KEY_B: BODIES[KEY_B]}
    result = _fetch(_cfg(), _Store(bodies=bodies))
    assert result.status == AdapterStatus.OK
    assert set(_by_id(result)) == {"event:e-3"}
    assert result.unavailable == ("body",)


def test_listing_failure_fails_the_source():
    def boom(bucket, prefix):
        raise RuntimeError("denied")

    result = s3_records.fetch(_cfg(), lister=boom, text_reader=lambda b, k: "", now=NOW)
    assert result.status == AdapterStatus.FAILED
    assert result.unavailable == ("source",)


def test_limit_and_order_bound_the_lines_of_one_object():
    result = _fetch(_cfg(limit=1, order="last"), _Store())
    assert set(_by_id(result)) == {"event:e-2", "event:e-3"}


def test_partition_key_is_configurable():
    store = _Store(listing={}, bodies={})
    _fetch(_cfg(days=1, partition_key="date"), store)
    assert store.listed == [f"{PREFIX}date=2026-10-15/"]


def test_malformed_bounds_fail_the_source():
    for extra, missing in (
        ({"days": 0}, ("days",)),
        ({"days": True}, ("days",)),
        ({"days": s3_records.MAX_JSONL_DAYS + 1}, ("days",)),
        ({"max_objects": "50"}, ("max_objects",)),
        ({"max_objects": s3_records.MAX_JSONL_OBJECTS + 1}, ("max_objects",)),
        ({"partition_key": "dt/x"}, ("partition_key",)),
        ({"partition_key": " "}, ("partition_key",)),
    ):
        store = _Store()
        result = _fetch(_cfg(**extra), store)
        assert result.status == AdapterStatus.FAILED, extra
        assert result.unavailable == missing, extra
        assert store.listed == [], extra


def test_a_fan_out_or_body_mode_on_jsonl_is_refused():
    for extra, missing in (({"records_path": "items"}, ("format",)),
                           ({"array_fields": ["a"]}, ("format",)),
                           ({"body": "text"}, ("body",))):
        result = _fetch(_cfg(**extra), _Store())
        assert result.status == AdapterStatus.FAILED, extra
        assert result.unavailable == missing, extra


def test_defaults_bound_the_read_when_undeclared():
    cfg = _cfg()
    del cfg["days"], cfg["max_objects"]
    store = _Store(listing={}, bodies={})
    _fetch(cfg, store)
    assert len(store.listed) == s3_records.DEFAULT_JSONL_DAYS


def test_parse_jsonl_counts_only_non_blank_bad_lines():
    records, bad = parse_jsonl(b'{"a": 1}\n\n  \n"str"\n{"b": 2}\r\n')
    assert records == [{"a": 1}, {"b": 2}]
    assert bad == 1
    assert parse_jsonl("") == ([], 0)
