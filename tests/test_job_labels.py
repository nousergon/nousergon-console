"""Job labels: a folded run row is named by its job's readable name.

A run list folds each job's runs to the newest one and names the folded row
by its job (alpha-engine-config-I11805). For the Director's pane that job is
the id before `@`, so the newest evening plan read `director:plan`, the
nightly run's heartbeat `director:run-heartbeat`. `job_label` lets the source
declare the job's name ("Evening plan", "Nightly run: heartbeat") while every
id, and every URL built from one, stays exactly as it was.
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import s3_records
from console.index.graph import Index
from console.model.envelope import AdapterStatus
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve

NOW = datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)
PREFIX = "ops/daily-summaries/"

PLAN_KEYS = [
    (f"{PREFIX}plan-2026-10-22.md", "2026-10-22T22:05:50+00:00"),
    (f"{PREFIX}plan-2026-10-21.md", "2026-10-21T22:14:09+00:00"),
]
MARKER_KEYS = [
    (f"{PREFIX}run-2026-10-22.started", "2026-10-23T02:09:36+00:00"),
    (f"{PREFIX}run-2026-10-22.heartbeat", "2026-10-23T03:55:54+00:00"),
    (f"{PREFIX}run-2026-10-21.ended", "2026-10-22T12:02:55+00:00"),
]
BODIES = {
    f"{PREFIX}run-2026-10-22.started": {"started_utc": "2026-10-23T02:09:36Z"},
    f"{PREFIX}run-2026-10-22.heartbeat": {"at": "2026-10-23T03:55:54Z", "event": "C32"},
    f"{PREFIX}run-2026-10-21.ended": {"ended_utc": "2026-10-22T12:02:55Z"},
}


def _plan_cfg(**extra):
    return {
        "bucket": "b", "prefix": PREFIX, "kind": "run", "format": "object",
        "key_pattern": r"^ops/daily-summaries/(?P<record>plan)-(?P<cycle>\d{4}-\d{2}-\d{2})\.md$",
        "id_template": "director:{record}@{cycle}",
        "as_of_field": "last_modified", "state_default": "HEALTHY",
        "facets": {"pane": {"value": "director"}},
        "date_fields": {"night": {"path": "cycle"}},
        "label_template": "Evening plan for the {night:%a %-m/%-d} night",
        "job_label": "Evening plan",
        **extra,
    }


def _marker_cfg(**extra):
    return {
        "bucket": "b", "prefix": PREFIX, "kind": "run",
        "key_pattern": r"^ops/daily-summaries/run-(?P<cycle>\d{4}-\d{2}-\d{2})\.(?P<marker>started|heartbeat|ended)$",
        "id_template": "director:run-{marker}@{cycle}",
        "state_default": "HEALTHY",
        "facets": {"pane": {"value": "director"}},
        "job_label": "Nightly run: {marker}",
        **extra,
    }


def _fetch(cfg, keys):
    return s3_records.fetch(cfg, lister=lambda b, p: keys,
                            reader=lambda b, k: BODIES.get(k), now=NOW)


def _index(*results) -> Index:
    index = Index()
    for res in results:
        index.add_result(res)
    index.finalize()
    return index


def _director_index() -> Index:
    return _index(_fetch(_plan_cfg(), PLAN_KEYS), _fetch(_marker_cfg(), MARKER_KEYS))


def _page(index, route):
    path, _, query = route.partition("?")
    return _html(index, resolve(path, query))


# ----------------------------------------------------------------- adapter --

def test_a_row_carries_its_jobs_name_and_keeps_its_id():
    res = _fetch(_plan_cfg(), PLAN_KEYS)
    assert res.status is AdapterStatus.OK and res.unavailable == ()
    by_id = {e.id: e for e in res.entities}
    ent = by_id["director:plan@2026-10-22"]
    assert ent.detail["job_label"] == "Evening plan"
    assert ent.detail["label"] == "Evening plan for the Thu 10/22 night"


def test_a_job_label_formats_against_the_key_names_in_a_parsed_format():
    # `format: json` (the default): the key's named groups still reach it.
    res = _fetch(_marker_cfg(), MARKER_KEYS)
    assert res.status is AdapterStatus.OK and res.unavailable == ()
    names = {e.id: e.detail["job_label"] for e in res.entities}
    assert names == {"director:run-started@2026-10-22": "Nightly run: started",
                     "director:run-heartbeat@2026-10-22": "Nightly run: heartbeat",
                     "director:run-ended@2026-10-21": "Nightly run: ended"}


def test_no_job_label_means_no_job_name():
    cfg = _plan_cfg()
    cfg.pop("job_label")
    res = _fetch(cfg, PLAN_KEYS)
    assert res.unavailable == ()
    assert all("job_label" not in e.detail for e in res.entities)


def test_an_unresolvable_job_label_keeps_the_row_and_says_so():
    res = _fetch(_plan_cfg(job_label="{missing}"), PLAN_KEYS)
    assert res.status is AdapterStatus.OK
    assert len(res.entities) == 2
    assert all("job_label" not in e.detail for e in res.entities)
    assert res.unavailable == ("label",)


def test_a_malformed_job_label_fails_the_source():
    for bad in ("", "   ", 3, ["Evening plan"]):
        res = _fetch(_plan_cfg(job_label=bad), PLAN_KEYS)
        assert res.status is AdapterStatus.FAILED
        assert res.unavailable == ("job_label",)


def test_one_adapter_minting_two_jobs_names_each_from_a_map():
    keys = PLAN_KEYS + [(f"{PREFIX}run-2026-10-21.md", "2026-10-22T14:35:50+00:00")]
    cfg = _plan_cfg(
        key_pattern=r"^ops/daily-summaries/(?P<record>run|plan)-(?P<cycle>\d{4}-\d{2}-\d{2})\.md$",
        label_template=None,
        job_label={"director:plan": "Evening plan", "director:run": "Nightly run record"})
    res = _fetch(cfg, keys)
    assert res.status is AdapterStatus.OK and res.unavailable == ()
    names = {e.id: e.detail["job_label"] for e in res.entities}
    assert names == {"director:plan@2026-10-22": "Evening plan",
                     "director:plan@2026-10-21": "Evening plan",
                     "director:run@2026-10-21": "Nightly run record"}


def test_a_job_the_map_leaves_out_keeps_its_id_and_says_so():
    keys = PLAN_KEYS + [(f"{PREFIX}run-2026-10-21.md", "2026-10-22T14:35:50+00:00")]
    cfg = _plan_cfg(
        key_pattern=r"^ops/daily-summaries/(?P<record>run|plan)-(?P<cycle>\d{4}-\d{2}-\d{2})\.md$",
        label_template=None, job_label={"director:plan": "Evening plan"})
    res = _fetch(cfg, keys)
    by_id = {e.id: e for e in res.entities}
    assert "job_label" not in by_id["director:run@2026-10-21"].detail
    assert res.unavailable == ("label",)


def test_a_malformed_job_label_map_fails_the_source():
    for bad in ({}, {"director:plan": ""}, {"director:plan@2026-10-22": "Evening plan"},
                {"director:plan": 3}, {"": "Evening plan"}):
        res = _fetch(_plan_cfg(job_label=bad), PLAN_KEYS)
        assert res.status is AdapterStatus.FAILED, bad
        assert res.unavailable == ("job_label",)


# -------------------------------------------------------------------- list --

def test_the_folded_director_pane_names_each_job_readably():
    page = _page(_director_index(), "/run?pane=director")
    assert "4 jobs from 5 runs" in page
    for name in ("Evening plan", "Nightly run: started", "Nightly run: heartbeat",
                 "Nightly run: ended"):
        assert f">{name}</a>" in page
    # The newest plan is the row, still linked by its id; the older one is folded.
    assert 'href="/run/director:plan@2026-10-22"' in page
    assert "director:plan@2026-10-21" not in page
    assert ">director:plan</a>" not in page


def test_json_carries_the_same_names():
    index = _director_index()
    doc = render_json.payload(index, resolve("/run", "pane=director"))
    assert doc["latest_per_job"] == {"jobs": 4, "runs": 5}
    assert doc["labels"]["director:plan@2026-10-22"] == "Evening plan"
    assert doc["labels"]["director:run-heartbeat@2026-10-22"] == "Nightly run: heartbeat"
    listing = render_json.list_rows(index, resolve("/run", "pane=director"))
    assert listing.labels == doc["labels"]


def test_every_run_names_each_run_by_its_own_label():
    # Unfolded, a row is one run, not a job: it shows the run's label.
    page = _page(_director_index(), "/run?pane=director&runs=all")
    assert ">Evening plan for the Thu 10/22 night</a>" in page
    assert ">Evening plan for the Wed 10/21 night</a>" in page


def test_a_job_without_a_declared_name_is_still_named_by_its_job():
    cfg = _plan_cfg()
    cfg.pop("job_label")
    index = _index(_fetch(cfg, PLAN_KEYS))
    listing = render_json.list_rows(index, resolve("/run", "pane=director"))
    assert listing.labels == {"director:plan@2026-10-22": "director:plan"}


def test_the_landing_fold_uses_the_job_name_too():
    index = _director_index()
    view = render_json.latest_per_job(index, list(index.all()), per_state=True)
    assert view.labels["director:run-ended@2026-10-21"] == "Nightly run: ended"


def test_search_finds_a_job_by_the_name_its_list_shows():
    page = _page(_director_index(), "/search?q=evening+plan")
    assert "no matches" not in page
    # Both plans match by their job's name and fold to the newest, named by it.
    assert '<a href="/run/director:plan@2026-10-22">Evening plan (2 runs)</a>' in page
    assert "2 by name" in page
    doc = render_json.payload(_director_index(), resolve("/search", "q=nightly run heartbeat"))
    assert [(h["entity"]["id"], h["matched_on"]) for h in doc["hits"]] == [
        ("director:run-heartbeat@2026-10-22", "label")]
