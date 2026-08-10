"""object-store-records adapter tests — recorded body fixtures, no live bucket."""
from __future__ import annotations

from console.adapters import object_store_records as osr
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind


UNIVERSE_BODY = {
    "as_of": "2026-08-09T12:00:00Z",
    "stocks": [
        {"ticker": "ACME", "sector": "technology", "gate": {"quant_filter_pass": True}},
        {"ticker": "BETA", "sector": "healthcare", "gate": {"quant_filter_pass": False}},
    ],
}

TRAJECTORY_BODY = {
    "as_of": "2026-08-09T00:00:00Z",
    "stocks": [
        {"ticker": "ACME", "sector": "technology", "rising": True, "attr_slope": 0.4},
    ],
}


def _reader(body_by_key):
    def reader(bucket, key):
        assert bucket == "fixture-bucket"
        return body_by_key[key]
    return reader


def _cfg(**overrides):
    base = {
        "bucket": "fixture-bucket",
        "keys": ["scanner/universe/latest.json"],
        "entity_kind": "artifact",
        "records_path": "stocks",
        "id_template": "{ticker}",
        "body_as_of_field": "as_of",
    }
    base.update(overrides)
    return base


def test_one_entity_per_record_keyed_by_ticker():
    result = osr.fetch(_cfg(), reader=_reader({"scanner/universe/latest.json": UNIVERSE_BODY}))
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert ids == {"ACME", "BETA"}
    assert all(e.kind is Kind.ARTIFACT for e in result.entities)


def test_body_as_of_applied_to_every_record():
    result = osr.fetch(_cfg(), reader=_reader({"scanner/universe/latest.json": UNIVERSE_BODY}))
    assert all(e.provenance.as_of == "2026-08-09T12:00:00Z" for e in result.entities)


def test_nested_state_field_dotted_path():
    result = osr.fetch(
        _cfg(state_field="gate.quant_filter_pass"),
        reader=_reader({"scanner/universe/latest.json": UNIVERSE_BODY}),
    )
    by_id = {e.id: e for e in result.entities}
    assert by_id["ACME"].state == "True"
    assert by_id["BETA"].state == "False"


def test_default_state_when_no_state_field_configured():
    result = osr.fetch(_cfg(), reader=_reader({"scanner/universe/latest.json": UNIVERSE_BODY}))
    assert all(e.state == "reporting" for e in result.entities)


def test_facets_extracted_from_record():
    result = osr.fetch(
        _cfg(facets={"sector": "sector"}),
        reader=_reader({"scanner/universe/latest.json": UNIVERSE_BODY}),
    )
    by_id = {e.id: e for e in result.entities}
    assert by_id["ACME"].facets == {"sector": "technology"}


def test_detail_carries_nested_record_untouched():
    result = osr.fetch(_cfg(), reader=_reader({"scanner/universe/latest.json": UNIVERSE_BODY}))
    by_id = {e.id: e for e in result.entities}
    assert by_id["ACME"].detail["gate"] == {"quant_filter_pass": True}


def test_body_level_as_of_injected_into_id_template_when_record_lacks_it():
    result = osr.fetch(
        _cfg(
            keys=["scanner/universe/trajectory/latest.json"],
            entity_kind="signal",
            id_template="{ticker}:{as_of}",
        ),
        reader=_reader({"scanner/universe/trajectory/latest.json": TRAJECTORY_BODY}),
    )
    (entity,) = result.entities
    assert entity.id == "ACME:2026-08-09T00:00:00Z"
    assert entity.kind is Kind.SIGNAL


def test_record_level_as_of_is_never_overwritten_by_body_level():
    body = {"as_of": "2026-08-09T00:00:00Z", "stocks": [{"ticker": "ACME", "as_of": "record-own"}]}
    result = osr.fetch(
        _cfg(id_template="{ticker}:{as_of}"),
        reader=_reader({"scanner/universe/latest.json": body}),
    )
    (entity,) = result.entities
    assert entity.id == "ACME:record-own"


def test_component_and_run_kinds_are_rejected():
    result = osr.fetch(_cfg(entity_kind="run"))
    assert result.status is AdapterStatus.FAILED
    assert "entity_kind:run" in result.unavailable[0]


def test_missing_records_path_in_body_marks_key_unavailable_not_a_crash():
    result = osr.fetch(
        _cfg(),
        reader=_reader({"scanner/universe/latest.json": {"as_of": "x", "not_stocks": []}}),
    )
    assert result.status is AdapterStatus.OK
    assert result.entities == ()
    assert "scanner/universe/latest.json:records_path" in result.unavailable


def test_reader_exception_for_one_key_does_not_fail_others():
    def reader(bucket, key):
        if key == "bad.json":
            raise RuntimeError("boom")
        return UNIVERSE_BODY

    result = osr.fetch(_cfg(keys=["bad.json", "scanner/universe/latest.json"]), reader=reader)
    assert result.status is AdapterStatus.OK
    assert "bad.json" in result.unavailable
    assert len(result.entities) == 2


def test_all_keys_unreadable_fails():
    def reader(bucket, key):
        raise RuntimeError("boom")

    result = osr.fetch(_cfg(), reader=reader)
    assert result.status is AdapterStatus.FAILED


def test_missing_required_config_fails():
    result = osr.fetch({"bucket": "fixture-bucket"})
    assert result.status is AdapterStatus.FAILED
