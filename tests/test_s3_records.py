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
