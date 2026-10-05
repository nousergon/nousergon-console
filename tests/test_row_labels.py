"""Row labels: a source names its rows for a reader without touching their ids.

The Director's reports are keyed by CYCLE (the evening that started the
night), so the morning report published on 10-05 is `2026-10-04.md` and its
row read `report:2026-10-04:morning-report`, which looked like a missing
10-05 report. `label_template` with `date_fields` lets the source say
"Mon 10/5 morning report (covers the 10/4 night)" while the id, and every URL
built from it, stays exactly as it was.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from console.adapters import s3_records
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.records_shape import resolve_dates, resolve_label, valid_date_fields
from console.render import html as render_html
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve

NOW = datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc)
PREFIX = "ops/daily-summaries/"
KEYS = [
    (f"{PREFIX}2026-10-04.md", "2026-10-05T13:30:01+00:00"),
    (f"{PREFIX}2026-10-03.md", "2026-10-04T13:29:39+00:00"),
]


def _morning_cfg(**extra):
    return {
        "bucket": "b", "prefix": PREFIX, "kind": "run", "format": "object",
        "key_pattern": r"^ops/daily-summaries/(?P<cycle>\d{4}-\d{2}-\d{2})\.md$",
        "id_template": "report:{cycle}:morning-report",
        "as_of_field": "last_modified", "state_default": "HEALTHY",
        "date_fields": {"published": {"path": "cycle", "offset_days": 1},
                        "night": {"path": "cycle"}},
        "label_template": "{published:%a %-m/%-d} morning report (covers the {night:%-m/%-d} night)",
        **extra,
    }


def _fetch(cfg, keys=KEYS):
    return s3_records.fetch(cfg, lister=lambda b, p: keys,
                            reader=lambda b, k: None, now=NOW)


# ----------------------------------------------------------------- adapter --

def test_a_morning_report_is_labelled_by_the_day_it_was_published():
    res = _fetch(_morning_cfg())
    assert res.status is AdapterStatus.OK and res.unavailable == ()
    by_id = {e.id: e for e in res.entities}
    # The id is untouched: old /run/report:... links still resolve.
    ent = by_id["report:2026-10-04:morning-report"]
    assert ent.detail["label"] == "Mon 10/5 morning report (covers the 10/4 night)"
    assert by_id["report:2026-10-03:morning-report"].detail["label"] == \
        "Sun 10/4 morning report (covers the 10/3 night)"


def test_a_label_can_use_id_names_and_a_timestamp_field():
    cfg = _morning_cfg(date_fields={"at": {"path": "last_modified"}},
                       label_template="{cycle} report, written {at:%Y-%m-%d}")
    ent = _fetch(cfg, KEYS[:1]).entities[0]
    assert ent.detail["label"] == "2026-10-04 report, written 2026-10-05"


def test_a_negative_offset_crosses_a_month_boundary():
    cfg = _morning_cfg(date_fields={"d": {"path": "cycle", "offset_days": -4}},
                       label_template="{d:%m/%d}")
    ent = _fetch(cfg, [(f"{PREFIX}2026-10-02.md", None)]).entities[0]
    assert ent.detail["label"] == "09/28"


def test_no_label_template_means_no_label():
    cfg = _morning_cfg()
    cfg.pop("label_template")
    res = _fetch(cfg)
    assert res.unavailable == ()
    assert all("label" not in e.detail for e in res.entities)


def test_an_unresolvable_label_keeps_the_row_by_its_id_and_says_so():
    for cfg in (_morning_cfg(label_template="{missing}"),
                _morning_cfg(date_fields={"d": {"path": "nope"}}),
                _morning_cfg(date_fields={"d": {"path": "key"}}),  # not a date
                _morning_cfg(label_template="{cycle:%a}")):  # str takes no strftime spec
        res = _fetch(cfg)
        assert res.status is AdapterStatus.OK
        assert {e.id for e in res.entities} == {"report:2026-10-04:morning-report",
                                               "report:2026-10-03:morning-report"}
        assert all("label" not in e.detail for e in res.entities)
        assert res.unavailable == ("label",)


def test_a_malformed_declaration_fails_the_source():
    bad_templates = ("", "   ", 3)
    for bad in bad_templates:
        res = _fetch(_morning_cfg(label_template=bad))
        assert res.status is AdapterStatus.FAILED
        assert res.unavailable == ("label_template",)
    bad_dates = ([], {"d": "cycle"}, {"d": {}}, {"d": {"path": ""}},
                 {"d": {"path": "cycle", "offset_days": "1"}},
                 {"d": {"path": "cycle", "offset_days": True}},
                 {"d": {"path": "cycle", "offset": 1}}, {"": {"path": "cycle"}})
    for bad in bad_dates:
        res = _fetch(_morning_cfg(date_fields=bad))
        assert res.status is AdapterStatus.FAILED, bad
        assert res.unavailable == ("date_fields",)


# ------------------------------------------------------------------ shape --

def test_shape_helpers():
    assert valid_date_fields({"d": {"path": "a.b", "offset_days": -1}})
    assert resolve_dates(None, {}) == {}
    assert resolve_dates({"d": {"path": "a.b"}}, {"a": {"b": "2026-10-04T23:21:24Z"}}) == \
        {"d": date(2026, 10, 4)}
    assert resolve_label("{x} {d:%d}", {"x": "y"}, {"d": date(2026, 1, 2)}) == "y 02"
    # A declared date wins a name collision with the record.
    assert resolve_label("{cycle:%d}", {"cycle": "2026-10-04"},
                         {"cycle": date(2026, 10, 5)}) == "05"
    assert resolve_label("  ", {}, {}) is None
    assert resolve_label("{0}", {}, {}) is None


# ---------------------------------------------------------------- rendering --

def _report(rid: str, age_days: float, label: str | None) -> Entity:
    detail: dict = {"window_days": 7}
    if label is not None:
        detail["label"] = label
    return Entity(kind=Kind.RUN, id=rid, state=State.HEALTHY,
                  provenance=Provenance(source="s3://b/reports/",
                                        as_of=(datetime.now(timezone.utc)
                                               - timedelta(days=age_days)).isoformat()),
                  facets={"pane": "reports"}, detail=detail)


def _index(*entities: Entity) -> Index:
    index = Index()
    index.add_result(AdapterResult(
        name="f", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=entities,
    ))
    index.finalize()
    return index


def _page(index, route):
    path, _, query = route.partition("?")
    return _html(index, resolve(path, query))


LABEL = "Mon 10/5 morning report (covers the 10/4 night)"


def _reports():
    return _index(_report("report:2026-10-04:morning-report", 0.2, LABEL),
                  _report("report:2026-10-04:evening-plan", 0.8, None))


def test_a_dated_list_shows_the_label_and_still_links_the_id():
    page = _page(_reports(), "/run?pane=reports")
    assert f'href="/run/report:2026-10-04:morning-report">{LABEL}</a>' in page
    # An unlabelled row reads by its id, as before.
    assert ">report:2026-10-04:evening-plan</a>" in page
    archive = _page(_reports(), "/run?pane=reports&days=all")
    assert f">{LABEL}</a>" in archive


def test_json_carries_declared_labels_on_a_dated_list():
    req = resolve("/run", "pane=reports")
    listing = render_json.list_rows(_reports(), req)
    assert listing.labels == {"report:2026-10-04:morning-report": LABEL}
    doc = render_json.payload(_reports(), req)
    assert doc["labels"] == {"report:2026-10-04:morning-report": LABEL}
    # The entity's own JSON carries it too, as detail.
    assert render_json.entity(listing.rows[0])["detail"]["label"] == LABEL


def test_an_undated_run_list_keeps_a_self_named_rows_label():
    ent = Entity(kind=Kind.RUN, id="report:x", state=State.HEALTHY,
                 provenance=Provenance(source="s", as_of="2026-10-05T00:00:00Z"),
                 detail={"label": "nice name"})
    other = Entity(kind=Kind.RUN, id="job@2026-10-05T00:00:00Z", state=State.HEALTHY,
                   provenance=Provenance(source="s", as_of="2026-10-05T00:00:00Z"),
                   detail={"label": "ignored for a job row"})
    listing = render_json.list_rows(_index(ent, other), resolve("/run"))
    assert listing.collapsed
    # A collapsed row standing for a job is named by its job.
    assert listing.labels == {"report:x": "nice name", "job@2026-10-05T00:00:00Z": "job"}
    every = render_json.list_rows(_index(ent, other), resolve("/run", "runs=all"))
    assert every.labels == {"report:x": "nice name",
                            "job@2026-10-05T00:00:00Z": "ignored for a job row"}


def test_the_entity_page_is_headed_by_the_label_and_keeps_the_id():
    index = _reports()
    ent = index.entity("report:2026-10-04:morning-report")
    page = render_html.entity_page(index, ent)
    assert f"<h1>{LABEL}</h1>" in page
    assert "&rsaquo; report:2026-10-04:morning-report</nav>" in page
    plain = render_html.entity_page(index, index.entity("report:2026-10-04:evening-plan"))
    assert "<h1>report:2026-10-04:evening-plan</h1>" in plain
