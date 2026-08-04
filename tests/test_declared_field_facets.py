from console.model.entity import Entity, Provenance
from console.model.kinds import Kind, State
from console.render.json import filter_entities


def _component(name, value, baseline=10):
    return Entity(kind=Kind.COMPONENT, id=name, state=State.HEALTHY,
                  provenance=Provenance(source="test"), detail={"fields": {
                      "rows_written": {"value": value, "baseline": baseline,
                                       "unit": "rows", "render": "count"}}})


def test_declared_numeric_field_filters_below_baseline():
    below, above = _component("below", 9), _component("above", 11)
    assert [e.id for e in filter_entities([below, above], {"rows_written": "below-baseline"})] == ["below"]


def test_field_without_baseline_does_not_admit_comparison_facet():
    no_baseline = _component("telemetry", 9, baseline=None)
    assert filter_entities([no_baseline], {"rows_written": "below-baseline"}) == []
