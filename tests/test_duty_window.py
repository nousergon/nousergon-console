"""§8.3 OFF_DUTY — a substrate proven powered down by its own duty anchor
(`alpha-engine-config-I12940`).

Built from the two real adapters the live surface uses — `yaml-directory`
over registry rows declaring `duty_anchor`, and `checks-envelope` over
`ops/checks/<id>/latest.json` bodies — and asserted on the merged index as
`Index.finalize()` produces it, so the wiring is exercised, not just the
function.

The live case: the laptop sleeps overnight, its heartbeat publisher
(`ai-nousergon-observability-heartbeat`) stops, and ~40 launchd rows read
MISSED/stale. The cases it must never allow: a child that dies while the
laptop is awake hidden as OFF_DUTY, and the anchor itself folding away.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from console.adapters import checks_envelope, yaml_directory
from console.index.duty_window import OFF_DUTY_VALUE, resolve_duty_window_state
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.kinds import Kind, State
from console.render.html import is_exception

ANCHOR = "ai-nousergon-observability-heartbeat"
CHILD = "ai-nousergon-llm-egress-proxy-8971"
OTHER = "ai-nousergon-laptop-cert-renew"
NOW = datetime(2026, 11, 2, 9, 25, tzinfo=timezone.utc)
SLEPT_AT = datetime(2026, 11, 2, 3, 35, tzinfo=timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _registry(tmp_path, rows: dict[str, str | None]):
    for cid, anchor in rows.items():
        body = (f"component_id: {cid}\nlifecycle: in-service\n"
                "substrate: laptop-launchd\ncadence_minutes: 15\n")
        if anchor:
            body += f"duty_anchor: {anchor}\n"
        (tmp_path / f"{cid}.yaml").write_text(body)
    return yaml_directory.fetch({"path": str(tmp_path), "id_field": "component_id"})


def _envelopes(runs: dict[str, tuple[datetime, int]]):
    """{check_id: (ran_at, cadence_minutes)} -> a checks-envelope result."""
    bodies = {
        f"ops/checks/{cid}/latest.json": {
            "schema_version": 1, "check_id": cid, "ran_at": _iso(ran),
            "status": "ok", "cadence_minutes": cad, "findings": [],
        }
        for cid, (ran, cad) in runs.items()
    }
    lister = lambda bucket, prefix: [(k, b["ran_at"]) for k, b in bodies.items()]
    reader = lambda bucket, key: bodies[key]
    return checks_envelope.fetch(
        {"bucket": "b", "prefix": "ops/checks/", "staleness_factor": 1.5},
        lister=lister, reader=reader, now=NOW,
    )


def _build(tmp_path, runs, anchors=None) -> Index:
    anchors = anchors if anchors is not None else {CHILD: ANCHOR, OTHER: ANCHOR, ANCHOR: None}
    idx = Index()
    idx.add_result(_registry(tmp_path, anchors))
    idx.add_result(_envelopes(runs))
    return idx.finalize()


def _key(cid: str) -> str:
    return f"ops/checks/{cid}/latest.json"


# --------------------------------------------------------------- the live case

def test_laptop_asleep_children_go_off_duty_and_leave_the_exception_list(tmp_path):
    idx = _build(tmp_path, {
        ANCHOR: (SLEPT_AT, 15),
        CHILD: (SLEPT_AT - timedelta(minutes=1), 15),
        OTHER: (SLEPT_AT - timedelta(minutes=20), 15),
    })
    for cid in (CHILD, OTHER):
        ent = idx.entity(cid)
        assert ent.state is State.OFF_DUTY
        assert ent.detail["off_duty_anchor"] == ANCHOR
        assert not is_exception(ent)
        art = idx.entity(_key(cid))
        assert art.state == OFF_DUTY_VALUE
        assert not is_exception(art)


def test_the_anchor_never_folds_so_a_sleeping_or_dead_laptop_stays_visible(tmp_path):
    idx = _build(tmp_path, {ANCHOR: (SLEPT_AT, 15), CHILD: (SLEPT_AT, 15)})
    assert idx.entity(ANCHOR).state is State.MISSED
    assert is_exception(idx.entity(ANCHOR))
    assert idx.entity(_key(ANCHOR)).state == "stale"


# --------------------------------------------------- on duty: MISSED stays loud

def test_child_missed_while_the_anchor_is_current_stays_missed(tmp_path):
    idx = _build(tmp_path, {
        ANCHOR: (NOW - timedelta(minutes=5), 15),
        CHILD: (NOW - timedelta(hours=2), 15),
    })
    assert idx.entity(ANCHOR).state is State.HEALTHY
    assert idx.entity(CHILD).state is State.MISSED
    assert idx.entity(_key(CHILD)).state == "stale"


def test_child_already_overdue_before_the_laptop_slept_stays_missed(tmp_path):
    """Died at 01:00 while the heartbeat kept publishing until 03:35: a miss
    on duty. The laptop sleeping afterwards does not launder it."""
    idx = _build(tmp_path, {
        ANCHOR: (SLEPT_AT, 15),
        CHILD: (SLEPT_AT - timedelta(hours=2, minutes=35), 15),
    })
    assert idx.entity(CHILD).state is State.MISSED
    assert idx.entity(_key(CHILD)).state == "stale"


def test_no_duty_anchor_declared_stays_missed(tmp_path):
    idx = _build(tmp_path, {ANCHOR: (SLEPT_AT, 15), CHILD: (SLEPT_AT, 15)},
                 anchors={CHILD: None, ANCHOR: None})
    assert idx.entity(CHILD).state is State.MISSED


def test_unresolvable_anchor_stays_missed(tmp_path):
    idx = _build(tmp_path, {CHILD: (SLEPT_AT, 15)},
                 anchors={CHILD: "no-such-heartbeat"})
    assert idx.entity(CHILD).state is State.MISSED


def test_a_self_declared_anchor_never_folds(tmp_path):
    idx = _build(tmp_path, {CHILD: (SLEPT_AT, 15)}, anchors={CHILD: CHILD})
    assert idx.entity(CHILD).state is State.MISSED


def test_a_failed_child_is_not_folded(tmp_path):
    """Only MISSED moves: a child whose last envelope said `error` is FAILED
    on its own evidence, asleep or not."""
    idx = Index()
    idx.add_result(_registry(tmp_path, {CHILD: ANCHOR, ANCHOR: None}))
    res = _envelopes({ANCHOR: (SLEPT_AT, 15), CHILD: (NOW - timedelta(minutes=1), 15)})
    ents = tuple(
        e if e.id != CHILD or e.kind is not Kind.COMPONENT
        else Entity(kind=e.kind, id=e.id, state=State.FAILED,
                    provenance=e.provenance, detail=e.detail)
        for e in res.entities
    )
    idx.add_result(type(res)(**{**res.__dict__, "entities": ents}))
    idx.finalize()
    assert idx.entity(CHILD).state is State.FAILED


# ------------------------------------------------------------- §9.6 and keys

def test_off_duty_rows_disclose_their_age_to_staleness_honesty(tmp_path):
    idx = _build(tmp_path, {ANCHOR: (SLEPT_AT, 15), CHILD: (SLEPT_AT, 15)})
    honesty = idx.staleness_honesty(now=NOW)
    assert honesty["computable"] is True
    assert CHILD not in honesty["violations"]
    assert _key(CHILD) not in honesty["violations"]


def test_resolver_preserves_the_full_key_set_and_ignores_unparseable_times():
    prov = Provenance(source="s", as_of="not-a-time")
    ents = {
        ANCHOR: Entity(kind=Kind.COMPONENT, id=ANCHOR, state=State.MISSED,
                       provenance=Provenance(source="s", as_of=_iso(SLEPT_AT))),
        CHILD: Entity(kind=Kind.COMPONENT, id=CHILD, state=State.MISSED, provenance=prov,
                      detail={"duty_anchor": ANCHOR, "cadence_minutes": 15}),
    }
    out = resolve_duty_window_state(ents)
    assert set(out) == set(ents)
    assert out[CHILD].state is State.MISSED


def test_yaml_directory_carries_duty_anchor(tmp_path):
    result = _registry(tmp_path, {CHILD: ANCHOR})
    assert result.entities[0].detail.get("duty_anchor") == ANCHOR
