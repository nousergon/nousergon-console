"""s3-records adapter tests — recorded fixtures, no live bucket.

Covers the three source shapes it generalizes over (whole-body, list-of-dicts,
parallel-arrays) plus CSV rows, matching the four crucible-dashboard artifact
shapes migrated by nousergon-console#54 (eod_report, order_book_rationale,
optimizer_shadow, trades_full) without naming that source's real bucket/prefix
— fixtures use synthetic names only (this repo is public; no fleet topology).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from console.adapters import s3_records
from console.config import ADAPTERS
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = "fixture-bucket"


def _by_id(result):
    return {e.id: e for e in result.entities}


# ---------------------------------------------------------------------------
# Whole-body mode — one entity per key, no fan-out (eod_report shape)
# ---------------------------------------------------------------------------

EOD_REPORT_2026_08_07 = {
    "generated_at": "2026-08-07T21:05:00+00:00",
    "schema_version": "2.2",
    "summary": {"nav": 105000.0, "daily_return_pct": 0.8, "daily_alpha_pct": 0.3},
}
EOD_REPORT_2026_08_08 = {
    "generated_at": "2026-08-08T21:05:00+00:00",
    "schema_version": "2.2",
    "summary": {"nav": 106000.0, "daily_return_pct": 0.9, "daily_alpha_pct": 0.4},
}


def _whole_body_cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": "consolidated/",
        "kind": "cycle",
        "question": "What did the portfolio do over a selected window?",
        "key_pattern": r"consolidated/(?P<date>\d{4}-\d{2}-\d{2})/eod_report\.json$",
        "id_template": "{date}",
        "as_of_field": "generated_at",
        "cadence": "1d",
        "fields": {
            "nav": {"path": "summary.nav", "unit": "usd"},
            "daily_alpha_pct": {"path": "summary.daily_alpha_pct", "unit": "pct"},
        },
        **extra,
    }


def _whole_body_lister(bucket, prefix):
    return [
        ("consolidated/2026-08-07/eod_report.json", "2026-08-07T21:06:00+00:00"),
        ("consolidated/2026-08-08/eod_report.json", "2026-08-08T21:06:00+00:00"),
    ]


def _whole_body_reader(bucket, key):
    return {
        "consolidated/2026-08-07/eod_report.json": EOD_REPORT_2026_08_07,
        "consolidated/2026-08-08/eod_report.json": EOD_REPORT_2026_08_08,
    }[key]


def test_whole_body_one_entity_per_key_keyed_by_id_template():
    result = s3_records.fetch(
        _whole_body_cfg(), lister=_whole_body_lister, reader=_whole_body_reader, now=NOW,
    )
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert ids == {"2026-08-07", "2026-08-08"}
    assert all(e.kind is Kind.CYCLE for e in result.entities)


def test_whole_body_declared_fields_render_nested_path():
    result = s3_records.fetch(
        _whole_body_cfg(), lister=_whole_body_lister, reader=_whole_body_reader, now=NOW,
    )
    ent = _by_id(result)["2026-08-08"]
    fields = ent.detail["fields"]
    assert fields["nav"]["value"] == 106000.0
    assert fields["nav"]["unit"] == "usd"
    assert fields["daily_alpha_pct"]["value"] == 0.4


def test_question_rendered_as_synthetic_text_field():
    result = s3_records.fetch(
        _whole_body_cfg(), lister=_whole_body_lister, reader=_whole_body_reader, now=NOW,
    )
    ent = _by_id(result)["2026-08-07"]
    q = ent.detail["fields"]["question"]
    assert q["value"] == "What did the portfolio do over a selected window?"
    assert q["render"] == "text"


def test_as_of_field_overrides_last_modified():
    result = s3_records.fetch(
        _whole_body_cfg(), lister=_whole_body_lister, reader=_whole_body_reader, now=NOW,
    )
    ent = _by_id(result)["2026-08-07"]
    assert ent.provenance.as_of == "2026-08-07T21:05:00+00:00"


def test_freshness_state_default_when_no_state_field_declared():
    # Cycle is not a component-state kind, and no state_field/state_default is
    # configured — falls back to the object-store freshness convention (§5.5:
    # the three not-computable cases stay three facts). NOW is >1d past both
    # fixture as-ofs, so both render stale against the 1d cadence — proves the
    # mechanism is live rather than a permanently-green default.
    result = s3_records.fetch(
        _whole_body_cfg(), lister=_whole_body_lister, reader=_whole_body_reader, now=NOW,
    )
    ent = _by_id(result)["2026-08-08"]
    assert ent.state == "stale"

    fresher_now = datetime(2026, 8, 8, 21, 30, 0, tzinfo=timezone.utc)
    result2 = s3_records.fetch(
        _whole_body_cfg(), lister=_whole_body_lister, reader=_whole_body_reader, now=fresher_now,
    )
    assert _by_id(result2)["2026-08-08"].state == "fresh"


# ---------------------------------------------------------------------------
# List-of-dicts fan-out (order_book_rationale's `tickers` array shape)
# ---------------------------------------------------------------------------

OBR_ARTIFACT = {
    "calendar_date": "2026-08-10",
    "run_id": "2608100530",
    "tickers": [
        {"ticker": "AAA", "terminal_state": "approved_entry",
         "research": {"signal": "BUY", "score": 82.0}},
        {"ticker": "BBB", "terminal_state": "risk_blocked",
         "research": {"signal": "HOLD", "score": 40.0}},
    ],
}


def _list_cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": "trades/order_book_rationale/",
        "kind": "decision",
        "question": "Why is each ticker in its current order-book state today?",
        "key_pattern": r"trades/order_book_rationale/(?P<run_id>\d{10})\.json$",
        "records_path": "tickers",
        "id_template": "{calendar_date}:{ticker}",
        "state_field": "terminal_state",
        "fields": {
            "signal": {"path": "research.signal", "render": "text"},
            "score": {"path": "research.score", "unit": "score"},
        },
        **extra,
    }


def _list_lister(bucket, prefix):
    return [("trades/order_book_rationale/2608100530.json", "2026-08-10T05:31:00+00:00")]


def _list_reader(bucket, key):
    return dict(OBR_ARTIFACT)


def test_list_of_dicts_fans_out_one_entity_per_record():
    result = s3_records.fetch(_list_cfg(), lister=_list_lister, reader=_list_reader, now=NOW)
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert ids == {"2026-08-10:AAA", "2026-08-10:BBB"}
    assert all(e.kind is Kind.DECISION for e in result.entities)


def test_list_of_dicts_record_state_field_verbatim():
    result = s3_records.fetch(_list_cfg(), lister=_list_lister, reader=_list_reader, now=NOW)
    by_id = _by_id(result)
    assert by_id["2026-08-10:AAA"].state == "approved_entry"
    assert by_id["2026-08-10:BBB"].state == "risk_blocked"


def test_list_of_dicts_nested_record_field_and_body_level_context_both_reachable():
    result = s3_records.fetch(_list_cfg(), lister=_list_lister, reader=_list_reader, now=NOW)
    aaa = _by_id(result)["2026-08-10:AAA"]
    assert aaa.detail["fields"]["signal"]["value"] == "BUY"
    assert aaa.detail["fields"]["score"]["value"] == 82.0


# ---------------------------------------------------------------------------
# Grouped fan-out (`records_path` with a `*` segment) — nousergon-console#57:
# a report card's per-tile MetricRecords (nested dict-then-array) and an
# apply-audit's per-loop outcomes (a dict OF records) are neither a plain
# array at one dotted path nor equal-length parallel arrays.
# ---------------------------------------------------------------------------

REPORT_CARD = {
    "run_date": "2026-08-08",
    "tiles": {
        "portfolio_outcome": {"components": [
            {"name": "beat_spy", "value": 0.4, "status": "GREEN"},
            {"name": "sharpe", "value": 1.1, "status": "WATCH"},
        ]},
        "evaluator_quality": {"components": [
            {"name": "rubric_drift", "value": 0.02, "status": "RED"},
        ]},
    },
}

APPLY_AUDIT = {
    "as_of": "2026-08-08",
    "schema_version": 1,
    "loops": {
        "executor_params": {"outcome": "promoted", "consecutive_blocked_weeks": 0},
        "scoring_weights": {"outcome": "blocked", "consecutive_blocked_weeks": 4},
    },
}


def _grouped_nested_cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": "evaluator/",
        "kind": "signal",
        "question": "What is the current institutional grade for each module/component, and why?",
        "key_pattern": r"evaluator/(?P<date>\d{4}-\d{2}-\d{2})/report_card\.json$",
        "records_path": "tiles.*.components",
        "group_field": "tile",
        "id_template": "{tile}:{name}",
        "state_field": "status",
        "fields": {"value": {"path": "value", "render": "value"}},
        **extra,
    }


def _grouped_dict_cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": "config/apply_audit/",
        "kind": "decision",
        "question": "Was this week's parameter change promoted, blocked, or gated — why?",
        "key_pattern": r"config/apply_audit/latest\.json$",
        "records_path": "loops.*",
        "group_field": "loop",
        "id_template": "{loop}:{as_of}",
        "state_field": "outcome",
        "fields": {"consecutive_blocked_weeks": {"path": "consecutive_blocked_weeks", "render": "count"}},
        **extra,
    }


def _one_key_lister(key, stamp):
    def lister(bucket, prefix):
        return [(key, stamp)]
    return lister


def _one_key_reader(body):
    def reader(bucket, key):
        return dict(body)
    return reader


def test_grouped_nested_dict_then_array_injects_the_group_key():
    cfg = _grouped_nested_cfg()
    result = s3_records.fetch(
        cfg,
        lister=_one_key_lister("evaluator/2026-08-08/report_card.json", "2026-08-08T21:00:00+00:00"),
        reader=_one_key_reader(REPORT_CARD), now=NOW,
    )
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert ids == {
        "portfolio_outcome:beat_spy", "portfolio_outcome:sharpe",
        "evaluator_quality:rubric_drift",
    }
    assert all(e.kind is Kind.SIGNAL for e in result.entities)


def test_grouped_nested_state_field_and_declared_value_field():
    cfg = _grouped_nested_cfg()
    result = s3_records.fetch(
        cfg,
        lister=_one_key_lister("evaluator/2026-08-08/report_card.json", "2026-08-08T21:00:00+00:00"),
        reader=_one_key_reader(REPORT_CARD), now=NOW,
    )
    by_id = _by_id(result)
    assert by_id["evaluator_quality:rubric_drift"].state == "RED"
    assert by_id["portfolio_outcome:sharpe"].detail["fields"]["value"]["value"] == 1.1


def test_grouped_dict_of_records_injects_the_group_key():
    cfg = _grouped_dict_cfg()
    result = s3_records.fetch(
        cfg,
        lister=_one_key_lister("config/apply_audit/latest.json", "2026-08-08T21:00:00+00:00"),
        reader=_one_key_reader(APPLY_AUDIT), now=NOW,
    )
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert ids == {"executor_params:2026-08-08", "scoring_weights:2026-08-08"}
    assert all(e.kind is Kind.DECISION for e in result.entities)


def test_grouped_dict_of_records_state_and_body_level_as_of_both_reachable():
    """`as_of` sits on the ENCLOSING body, not inside each loop record —
    reachable via the same body_root merge every other shape already uses;
    no separate mechanism needed for it."""
    cfg = _grouped_dict_cfg()
    result = s3_records.fetch(
        cfg,
        lister=_one_key_lister("config/apply_audit/latest.json", "2026-08-08T21:00:00+00:00"),
        reader=_one_key_reader(APPLY_AUDIT), now=NOW,
    )
    by_id = _by_id(result)
    assert by_id["scoring_weights:2026-08-08"].state == "blocked"
    assert by_id["scoring_weights:2026-08-08"].detail["fields"]["consecutive_blocked_weeks"]["value"] == 4


def test_grouped_mode_does_not_disturb_plain_records_path():
    """A `records_path` with no `*` still takes the old, direct-array path —
    the grouped walk is opt-in via the literal `*` segment only."""
    result = s3_records.fetch(_list_cfg(), lister=_list_lister, reader=_list_reader, now=NOW)
    assert result.status is AdapterStatus.OK
    assert len(result.entities) == 2


# ---------------------------------------------------------------------------
# Parallel-array fan-out (optimizer_shadow's tickers/target_weights/… shape)
# ---------------------------------------------------------------------------

SHADOW_2026_08_10 = {
    "run_date": "2026-08-10",
    "shadow_status": "ok",
    "portfolio_nav": 100000.0,
    "optimizer_cfg": {"risk_aversion": 4.0, "tcost_bps": 5.0},
    "diagnostics": {"portfolio_vol_ann": 0.12, "turnover_one_way": 0.05},
    "tickers": ["AAA", "BBB"],
    "eligibility": [True, False],
    "target_weights": [0.05, 0.0],
}


def _array_decision_cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": "predictor/optimizer_shadow/",
        "kind": "decision",
        "question": "Why did the MVO optimizer assign each stock its target weight?",
        "key_pattern": r"predictor/optimizer_shadow/(?P<date>\d{4}-\d{2}-\d{2})\.json$",
        "array_fields": ["tickers", "eligibility", "target_weights"],
        "id_template": "{date}:{tickers}",
        "state_field": "eligibility",
        "fields": {"target_weight": {"path": "target_weights", "unit": "ratio"}},
        **extra,
    }


def _array_lister(bucket, prefix):
    return [
        ("predictor/optimizer_shadow/2026-08-10.json", "2026-08-10T05:31:00+00:00"),
        ("predictor/optimizer_shadow/latest.json", "2026-08-10T05:31:00+00:00"),
    ]


def _array_reader(bucket, key):
    if key.endswith("latest.json"):
        return dict(SHADOW_2026_08_10)
    return dict(SHADOW_2026_08_10)


def test_array_fields_zip_index_wise_into_records():
    result = s3_records.fetch(
        _array_decision_cfg(), lister=_array_lister, reader=_array_reader, now=NOW,
    )
    ids = {e.id for e in result.entities}
    assert "2026-08-10:AAA" in ids
    assert "2026-08-10:BBB" in ids


def test_latest_json_sidecar_excluded_by_key_pattern():
    # The key_pattern requires a literal date segment — `latest.json` cannot
    # match it, mirroring the source's own skip-latest-sidecar convention
    # without this adapter knowing anything about that convention.
    result = s3_records.fetch(
        _array_decision_cfg(), lister=_array_lister, reader=_array_reader, now=NOW,
    )
    assert len(result.entities) == 2  # not 4 — latest.json contributed nothing


def test_array_record_state_is_the_zipped_scalar_verbatim():
    result = s3_records.fetch(
        _array_decision_cfg(), lister=_array_lister, reader=_array_reader, now=NOW,
    )
    by_id = _by_id(result)
    assert by_id["2026-08-10:AAA"].state == "True"
    assert by_id["2026-08-10:BBB"].state == "False"


def test_body_level_fields_reachable_from_a_fanned_out_record():
    """Signal-lens config on the SAME artifact — whole-body mode, flattening
    nested optimizer_cfg/diagnostics fields via dotted paths, no fan-out."""
    cfg = {
        "bucket": BUCKET,
        "prefix": "predictor/optimizer_shadow/",
        "kind": "signal",
        "question": "How have deployed risk levers and realized risk moved day to day?",
        "key_pattern": r"predictor/optimizer_shadow/(?P<date>\d{4}-\d{2}-\d{2})\.json$",
        "id_template": "{date}",
        "state_field": "shadow_status",
        "fields": {
            "risk_aversion": {"path": "optimizer_cfg.risk_aversion"},
            "portfolio_vol_ann": {"path": "diagnostics.portfolio_vol_ann", "unit": "ratio"},
        },
    }
    result = s3_records.fetch(cfg, lister=_array_lister, reader=_array_reader, now=NOW)
    ent = _by_id(result)["2026-08-10"]
    assert ent.kind is Kind.SIGNAL
    assert ent.state == "ok"
    assert ent.detail["fields"]["risk_aversion"]["value"] == 4.0
    assert ent.detail["fields"]["portfolio_vol_ann"]["value"] == 0.12


# ---------------------------------------------------------------------------
# CSV rows (trades_full.csv shape) — Run kind, a COMPONENT_STATE_KINDS member
# ---------------------------------------------------------------------------

TRADES_CSV = (
    "ticker,created_at,action,fill_price\n"
    "AAA,2026-08-10T14:31:00Z,ENTER,101.20\n"
    "BBB,2026-08-10T14:32:00Z,EXIT,55.10\n"
)


def _csv_cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": "",
        "kind": "run",
        "question": "What trades were executed, and how did fills compare to intended price?",
        "key_pattern": r"trades_full\.csv$",
        "format": "csv",
        "id_template": "{ticker}:{created_at}",
        "state_default": "HEALTHY",
        "fields": {"fill_price": {"path": "fill_price", "unit": "usd"}},
        **extra,
    }


def _csv_lister(bucket, prefix):
    return [("trades_full.csv", "2026-08-10T14:35:00Z")]


def _csv_reader(bucket, key):
    return TRADES_CSV


def test_csv_rows_become_one_entity_per_row():
    result = s3_records.fetch(_csv_cfg(), lister=_csv_lister, reader=_csv_reader, now=NOW)
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert ids == {"AAA:2026-08-10T14:31:00Z", "BBB:2026-08-10T14:32:00Z"}
    assert all(e.kind is Kind.RUN for e in result.entities)


def test_run_kind_state_maps_through_the_closed_vocabulary():
    result = s3_records.fetch(_csv_cfg(), lister=_csv_lister, reader=_csv_reader, now=NOW)
    ent = next(iter(result.entities))
    assert ent.state is State.HEALTHY  # a real State member — §5.1 structural rule


def test_run_kind_with_no_resolvable_state_is_unreported_not_dropped():
    cfg = _csv_cfg(state_default=None)
    result = s3_records.fetch(cfg, lister=_csv_lister, reader=_csv_reader, now=NOW)
    assert all(e.state is State.UNREPORTED for e in result.entities)


# ---------------------------------------------------------------------------
# Cross-cutting adapter contract
# ---------------------------------------------------------------------------

def test_missing_kind_is_failed():
    cfg = {k: v for k, v in _whole_body_cfg().items() if k != "kind"}
    result = s3_records.fetch(cfg, lister=_whole_body_lister, reader=_whole_body_reader, now=NOW)
    assert result.status is AdapterStatus.FAILED
    assert "kind" in result.unavailable


def test_unknown_kind_is_failed():
    cfg = _whole_body_cfg(kind="not-a-real-kind")
    result = s3_records.fetch(cfg, lister=_whole_body_lister, reader=_whole_body_reader, now=NOW)
    assert result.status is AdapterStatus.FAILED
    assert "kind" in result.unavailable


def test_missing_bucket_is_failed():
    cfg = {k: v for k, v in _whole_body_cfg().items() if k != "bucket"}
    result = s3_records.fetch(cfg, lister=_whole_body_lister, reader=_whole_body_reader, now=NOW)
    assert result.status is AdapterStatus.FAILED


def test_lister_failure_is_failed_not_empty():
    def boom(b, p):
        raise RuntimeError("bucket unreachable")
    result = s3_records.fetch(_whole_body_cfg(), lister=boom, reader=_whole_body_reader, now=NOW)
    assert result.status is AdapterStatus.FAILED
    assert "source" in result.unavailable
    assert result.entities == ()


def test_unreadable_body_is_partial_not_dropped_entirely():
    def bad_reader(b, k):
        raise RuntimeError("access denied")
    result = s3_records.fetch(
        _whole_body_cfg(), lister=_whole_body_lister, reader=bad_reader, now=NOW,
    )
    assert result.status is AdapterStatus.OK
    assert result.entities == ()
    assert "body" in result.unavailable


def test_malformed_records_path_is_partial_not_a_crash():
    cfg = _list_cfg(records_path="not_a_list_field")
    result = s3_records.fetch(cfg, lister=_list_lister, reader=_list_reader, now=NOW)
    assert result.status is AdapterStatus.OK
    assert result.entities == ()
    assert "body" in result.unavailable


def test_unmatched_keys_skipped():
    def lister(b, p):
        return [("other/path/file.json", "2026-08-10T00:00:00Z")]
    result = s3_records.fetch(_whole_body_cfg(), lister=lister, reader=_whole_body_reader, now=NOW)
    assert result.entities == ()


def test_no_lister_or_reader_is_failed(monkeypatch):
    monkeypatch.setattr(s3_records, "_default_s3", lambda: (None, None))
    result = s3_records.fetch(_whole_body_cfg(), now=NOW)
    assert result.status is AdapterStatus.FAILED
    assert "lister" in result.unavailable
    assert "reader" in result.unavailable


def test_s3_records_is_registered():
    assert "s3-records" in ADAPTERS


def test_produces_covers_every_kind_reachable_via_config():
    assert set(s3_records.produces) == {
        "component", "run", "cycle", "artifact", "signal", "decision", "incident",
    }


# ---------------------------------------------------------------------------
# state_map — folded in from `dated-snapshot` when the four record-shaped
# adapters were consolidated (nousergon-console#79, Brian ruling 2026-08-11).
#
# It was that adapter's ONE genuine capability. Without it this adapter can only
# read sources that already emit `observability-policy.md` §8.3's thirteen state
# names verbatim, which almost no real source does.
#
# The three outcomes below are three different facts (§5.5), and collapsing any
# pair of them is how a surface reports health it does not have.
# ---------------------------------------------------------------------------

RUN_BODY_PASSED = {"finished_at": "2026-08-08T02:00:00+00:00", "outcome": "passed"}
RUN_BODY_FAILED = {"finished_at": "2026-08-09T02:00:00+00:00", "outcome": "failed"}
RUN_BODY_WEIRD = {"finished_at": "2026-08-10T02:00:00+00:00", "outcome": "banana"}
RUN_BODY_SILENT = {"finished_at": "2026-08-10T02:00:00+00:00"}


def _run_cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": "runs/",
        "kind": "run",
        "key_pattern": r"runs/(?P<date>\d{4}-\d{2}-\d{2})/result\.json$",
        "id_template": "nightly@{date}",
        "as_of_field": "finished_at",
        "state_field": "outcome",
        **extra,
    }


def _run_fetch(body, **extra):
    return s3_records.fetch(
        _run_cfg(**extra),
        lister=lambda b, p: [("runs/2026-08-08/result.json", "2026-08-08T02:01:00+00:00")],
        reader=lambda b, k: body,
        now=NOW,
    )


def test_state_map_translates_the_sources_own_vocabulary():
    result = _run_fetch(RUN_BODY_PASSED, state_map={"passed": "HEALTHY", "failed": "FAILED"})
    assert list(_by_id(result).values())[0].state is State.HEALTHY


def test_state_map_translates_a_failure_too():
    result = _run_fetch(RUN_BODY_FAILED, state_map={"passed": "HEALTHY", "failed": "FAILED"})
    assert list(_by_id(result).values())[0].state is State.FAILED


def test_a_value_nothing_can_interpret_is_DEGRADED_not_unreported():
    """`dated-snapshot` resolved this to DEGRADED and this adapter to
    UNREPORTED. DEGRADED is the one kept: "reported something uninterpretable"
    is a finding, while UNREPORTED means nothing reported at all."""
    result = _run_fetch(RUN_BODY_WEIRD, state_map={"passed": "HEALTHY"})
    assert list(_by_id(result).values())[0].state is State.DEGRADED


def test_no_value_at_all_is_UNREPORTED_not_degraded():
    """The other half of the same distinction — and the reason both exist."""
    result = _run_fetch(RUN_BODY_SILENT, state_map={"passed": "HEALTHY"})
    assert list(_by_id(result).values())[0].state is State.UNREPORTED


def test_a_state_map_naming_a_nonexistent_state_is_DEGRADED_never_green():
    """The MAP is wrong, not the source. A typo'd target must not read as
    healthy — that is a fabricated green, produced by configuration."""
    result = _run_fetch(RUN_BODY_PASSED, state_map={"passed": "TOTALLY_FINE"})
    assert list(_by_id(result).values())[0].state is State.DEGRADED


def test_a_direct_state_name_still_resolves_without_any_map():
    """The pre-existing behaviour is unchanged: a source already emitting the
    thirteen needs no map, and adding one must not have made a map mandatory."""
    result = _run_fetch({"finished_at": "2026-08-08T02:00:00+00:00", "outcome": "healthy"})
    assert list(_by_id(result).values())[0].state is State.HEALTHY


def test_the_map_wins_over_a_coincidental_direct_match():
    """A source emitting `degraded` while its own map says that means FAILED is
    a real case — the declared translation is authoritative, not the accident
    that the raw value happens to be a state name."""
    result = _run_fetch(
        {"finished_at": "2026-08-08T02:00:00+00:00", "outcome": "degraded"},
        state_map={"degraded": "FAILED"},
    )
    assert list(_by_id(result).values())[0].state is State.FAILED


# ---------------------------------------------------------------------------
# facets — folded in from `object-store-records` (I79).
#
# Facets are what §2.2 filters on uniformly across the whole index, which makes
# them a different thing from declared `fields` (§5.8): fields are RENDERED,
# facets are FILTERED ON. An adapter without facets produces entities the index
# cannot narrow, which is why this could not simply be dropped.
# ---------------------------------------------------------------------------

UNIVERSE_BODY = {
    "as_of": "2026-08-08T13:00:00+00:00",
    "stocks": [
        {"ticker": "AAA", "sector": "tech", "score": 0.9},
        {"ticker": "BBB", "sector": "energy", "score": 0.4},
        {"ticker": "CCC", "score": 0.1},
    ],
}


def _universe_result(**extra):
    cfg = {
        "bucket": BUCKET,
        "prefix": "scanner/",
        "kind": "artifact",
        "key_pattern": r"scanner/universe/latest\.json$",
        "records_path": "stocks",
        "id_template": "{ticker}",
        "as_of_field": "as_of",
        "facets": {"sector": "sector"},
        **extra,
    }
    return s3_records.fetch(
        cfg,
        lister=lambda b, p: [("scanner/universe/latest.json", "2026-08-08T13:01:00+00:00")],
        reader=lambda b, k: UNIVERSE_BODY,
        now=NOW,
    )


def test_a_declared_facet_reaches_the_entity():
    entities = _by_id(_universe_result())
    assert entities["AAA"].facets["sector"] == "tech"
    assert entities["BBB"].facets["sector"] == "energy"


def test_a_facet_whose_path_resolves_to_nothing_is_OMITTED_not_empty():
    """An absent facet and a facet whose value is "" filter differently, and
    inventing the second is a fabricated fact about the record."""
    entities = _by_id(_universe_result())
    assert "sector" not in entities["CCC"].facets


def test_a_facet_can_read_a_body_level_field_not_only_a_record_one():
    """`path_root` merges body and record, so a facet may come from either —
    the case `object-store-records` handled via its own body/record merge."""
    entities = _by_id(_universe_result(facets={"sector": "sector", "cut": "as_of"}))
    assert entities["AAA"].facets["cut"] == "2026-08-08T13:00:00+00:00"


def test_no_facets_declared_leaves_the_entity_unfaceted_rather_than_failing():
    entities = _by_id(_universe_result(facets={}))
    assert entities["AAA"].facets == {}


def test_an_explicit_single_key_is_reachable_as_a_key_pattern():
    """The whole of `object-store-records`' `keys:` list capability: a literal
    key is a pattern that matches exactly one thing. This is why that adapter
    needed no feature folded in beyond facets."""
    result = _universe_result()
    assert {e.id for e in result.entities} == {"AAA", "BBB", "CCC"}
    assert result.status is AdapterStatus.OK


def test_a_declared_field_defaults_its_path_to_its_own_name():
    """Folded in from `dated-snapshot` (I79). A field named after the key it
    reads is the common case, and requiring `{path: x}` for a field called `x`
    is config noise that invites copy-paste errors."""
    entities = _by_id(_universe_result(fields={"score": {"render": "value"}}))
    assert entities["AAA"].detail["fields"]["score"]["value"] == 0.9


def test_an_explicit_path_still_wins_over_the_default():
    """No existing configuration may change meaning because of the default."""
    entities = _by_id(_universe_result(
        fields={"score": {"path": "sector", "render": "text"}}))
    assert entities["AAA"].detail["fields"]["score"]["value"] == "tech"


def test_a_field_matching_nothing_renders_None_rather_than_vanishing():
    """§5.5: a declared field whose source is absent is a fact about the
    record. Dropping it would make 'not declared' and 'declared but missing'
    the same shape."""
    entities = _by_id(_universe_result(fields={"nope": {"render": "value"}}))
    assert entities["AAA"].detail["fields"]["nope"]["value"] is None
