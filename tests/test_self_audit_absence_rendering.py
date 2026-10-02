"""A self-audit's monitoring surface: absence, staleness, malformed bodies and
failed runs must render non-green on the console itself, not only in a log.

This is the consumer half of a producer/consumer contract. A recurring audit
publishes ONE check envelope (schema_version 1, the fleet check-result shape).
Two adapters read that envelope:

- `checks-envelope` turns it into the audit's component row.
- `s3-records` fans the envelope's `panel` array out into per-measure
  `decision` rows that carry a `pane` facet.

A `declared-registry` declares the same pane row ids with
`default_state: unobserved`, so an audit that never published still renders
every row it owes as unobserved rather than as nothing.

The real adapters run here, through the real `Index` and renderers. Only the
store is a fixture. Names are synthetic, because this repo is public and must
carry no fleet topology.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import yaml

from console.adapters import checks_envelope, declared_registry, s3_records
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.render.html import is_exception, landing_exceptions

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = "fixture-bucket"
CHECK = "comp-self-audit"
KEY = f"ops/checks/{CHECK}/latest.json"
PANE = "self-audit"
MEASURES = ("last-attempt", "last-complete-run", "verdicts", "stale-evidence")


def _row(measure: str, state: str, reading: str, **fields) -> dict:
    return {"id": f"{PANE}:{measure}", "measure": measure, "console_state": state,
            "as_of": "2026-10-02T11:00:00+00:00", "reading": reading, **fields}


def _envelope(status: str, ran_at: str, panel: list[dict]) -> dict:
    return {
        "schema_version": 1, "check_id": CHECK, "label": "Self audit",
        "ran_at": ran_at, "status": status, "summary": f"{status} run",
        "cadence_minutes": 10080, "findings": [], "facets": {"pane": PANE},
        "panel": panel,
    }


PARTIAL = _envelope("attention", "2026-10-02T11:00:00+00:00", [
    _row("last-attempt", "DEGRADED", "published; run partial (exit 2)"),
    _row("last-complete-run", "UNREPORTED", "no complete run yet"),
    _row("verdicts", "DEGRADED", "0 compliant, 0 noncompliant, 40 unknown",
         unknown=40, compliant=0),
    _row("stale-evidence", "HEALTHY", "0 stale", unknown=0, compliant=0),
])
FAILED = _envelope("error", "2026-10-02T11:00:00+00:00", [
    _row("last-attempt", "FAILED", "rejected: report digest mismatch"),
    _row("last-complete-run", "UNREPORTED", "no complete run yet"),
    _row("verdicts", "UNREPORTED", "no verified run"),
    _row("stale-evidence", "UNREPORTED", "no verified run"),
])
# Last write was a clean `ok`, eleven days ago: the worker stopped.
STOPPED = _envelope("ok", "2026-09-21T11:00:00+00:00", [
    {**_row(m, "HEALTHY", "fine at the time"), "as_of": "2026-09-21T11:00:00+00:00"}
    for m in MEASURES
])

CHECKS_CFG = {
    "bucket": BUCKET, "prefix": "ops/checks/",
    "key_pattern": r"ops/checks/(?P<component_id>[^/]+)/latest\.json$",
    "staleness_factor": 1.5,
}
PANEL_CFG = {
    "bucket": BUCKET, "prefix": f"ops/checks/{CHECK}/",
    "key_pattern": rf"ops/checks/{CHECK}/latest\.json$",
    "kind": "decision", "records_path": "panel", "id_template": "{id}",
    "as_of_field": "as_of", "state_field": "console_state",
    "facets": {"pane": {"value": PANE}},
    "fields": {
        "reading": {"path": "reading", "render": "text"},
        "unknown": {"path": "unknown", "render": "count", "unit": "assessments",
                    "baseline": "none"},
    },
}


def _store(bodies: dict[str, object]):
    """`bodies` maps key -> dict (JSON body) or str (unparseable bytes)."""
    def lister(bucket, prefix):
        assert bucket == BUCKET
        return [(k, "2026-10-02T11:00:01+00:00") for k in bodies if k.startswith(prefix)]

    def reader(bucket, key):
        body = bodies[key]
        if isinstance(body, str):
            return json.loads(body)  # raises, as the real reader does
        return json.loads(json.dumps(body))
    return lister, reader


def _declared(tmp_path) -> AdapterResult:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"self_audit_pane": [
        {"id": f"{PANE}:{m}", "facets": {"pane": PANE}} for m in MEASURES
    ]}))
    return declared_registry.fetch({
        "path": str(path), "kind": "decision", "id_field": "id",
        "entries_field": "self_audit_pane", "default_state": "unobserved",
    })


def _registry_row() -> AdapterResult:
    """The component's observability-registry declaration (yaml-directory's
    role in a deployment), so absence is ABSENT-from-observation, not unknown."""
    return AdapterResult(
        name="registry", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id=CHECK, state=State.UNREPORTED,
                         provenance=Provenance(source="registry", as_of=None,
                                               evidence="fixture://registry")),),
    )


def _index(tmp_path, bodies: dict[str, object]) -> Index:
    lister, reader = _store(bodies)
    idx = Index()
    idx.add_result(_registry_row())
    idx.add_result(_declared(tmp_path))
    idx.add_result(checks_envelope.fetch(CHECKS_CFG, lister=lister, reader=reader, now=NOW))
    idx.add_result(s3_records.fetch(PANEL_CFG, lister=lister, reader=reader, now=NOW))
    idx.finalize()
    return idx


def _component(idx: Index) -> Entity:
    [ent] = [e for e in idx.all() if e.kind is Kind.COMPONENT and e.id == CHECK]
    return ent


def _pane(idx: Index) -> dict[str, object]:
    return {e.id.split(":", 1)[1]: e.state for e in idx.all()
            if e.kind is Kind.DECISION and e.facets.get("pane") == PANE}


def _dashboard(idx: Index, kind: str = "decision") -> dict:
    # The envelope's own `facets.pane` stamps the component row too, so the
    # pane gets one link per kind (component + decision).
    [entry] = [d for d in idx.landing_model().dashboards
               if d["pane"] == PANE and d["kind"] == kind]
    return entry


# ------------------------------------------------------------- absence --

def test_an_audit_that_never_published_renders_unreported_and_unobserved(tmp_path):
    idx = _index(tmp_path, {})
    assert _component(idx).state is State.UNREPORTED
    assert set(_pane(idx).values()) == {"unobserved"}
    assert set(_pane(idx)) == set(MEASURES)  # every owed row is still listed
    assert _component(idx) in landing_exceptions(idx)
    page = render_html.list_page(idx, Kind.DECISION, {"pane": PANE})
    assert page.count("unobserved") >= len(MEASURES)


def test_a_stopped_worker_goes_missed_even_though_its_last_write_was_ok(tmp_path):
    """The COMPONENT row carries staleness. A `decision` row with a declared
    `state_field` keeps the source's own value (`records_shape.resolve_state`)
    and is never aged, so the pane rows still read their last value. They
    render that value with its as-of, and the pane's own index entry carries
    the MISSED component, because the envelope's `facets.pane` stamps it."""
    idx = _index(tmp_path, {KEY: STOPPED})
    assert _component(idx).state is State.MISSED
    assert is_exception(_component(idx))
    assert _component(idx) in landing_exceptions(idx)
    assert _dashboard(idx, "component") == {
        "pane": PANE, "kind": "component", "url": f"/component?pane={PANE}",
        "rows": 1, "not_healthy": 1}
    assert set(_pane(idx).values()) == {"HEALTHY"}  # the documented limit
    page = render_html.list_page(idx, Kind.DECISION, {"pane": PANE})
    assert "2026-09-21" in page  # the age is on the row, not hidden


def test_a_malformed_envelope_renders_unreported_not_green(tmp_path):
    idx = _index(tmp_path, {KEY: "{not json"})
    assert _component(idx).state is State.UNREPORTED
    assert set(_pane(idx).values()) == {"unobserved"}  # the declaration holds


# ---------------------------------------------------- failed and partial --

def test_a_failed_run_is_failed_on_the_component_and_on_the_pane(tmp_path):
    idx = _index(tmp_path, {KEY: FAILED})
    assert _component(idx).state is State.FAILED
    pane = _pane(idx)
    assert pane["last-attempt"] == "FAILED"
    assert pane["verdicts"] == "UNREPORTED"
    assert _dashboard(idx)["not_healthy"] >= 1
    assert _dashboard(idx, "component")["not_healthy"] == 1
    page = render_html.list_page(idx, Kind.DECISION, {"pane": PANE})
    assert "rejected: report digest mismatch" in page


def test_a_partial_run_is_degraded_and_its_observed_rows_outrank_the_declaration(tmp_path):
    idx = _index(tmp_path, {KEY: PARTIAL})
    assert _component(idx).state is State.DEGRADED
    pane = _pane(idx)
    assert pane == {"last-attempt": "DEGRADED", "last-complete-run": "UNREPORTED",
                    "verdicts": "DEGRADED", "stale-evidence": "HEALTHY"}
    assert _dashboard(idx) == {"pane": PANE, "kind": "decision",
                               "url": f"/decision?pane={PANE}", "rows": 4,
                               "not_healthy": 3}  # UNREPORTED is not healthy
    page = render_html.list_page(idx, Kind.DECISION, {"pane": PANE})
    assert "<th>reading</th>" in page and "40 assessments" in page


def test_the_landing_view_carries_the_component_not_every_pane_row(tmp_path):
    """Home is "what's wrong now": the component row says the audit is wrong;
    the per-measure rows live on the pane, indexed from /dashboards."""
    idx = _index(tmp_path, {KEY: FAILED})
    exceptions = landing_exceptions(idx)
    assert _component(idx) in exceptions
    pane_rows = [e for e in exceptions if e.facets.get("pane") == PANE
                 and e.kind is Kind.DECISION]
    assert len(pane_rows) <= len(MEASURES)
    assert f'href="/decision?pane={PANE}"' in render_html.dashboards_page(idx)
