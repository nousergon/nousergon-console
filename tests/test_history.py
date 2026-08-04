from datetime import datetime, timezone

from console.history import query
from console.model.entity import Entity, Provenance
from console.model.kinds import Kind, State


def _entity():
    return Entity(kind=Kind.COMPONENT, id="writer", state=State.HEALTHY,
                  provenance=Provenance(source="fixture"), detail={
                      "history_retention_hours": 24,
                      "history": [{"as_of": "2026-08-04T11:00:00Z", "value": 4}],
                  })


def test_history_bounds_an_over_retention_request():
    result = query(_entity(), 48, now=datetime(2026, 8, 4, 12, tzinfo=timezone.utc))
    assert result["bounded"] is True
    assert result["requested_hours"] == 48
    assert result["effective_hours"] == result["retention_hours"] == 24
    assert result["points"] == [{"as_of": "2026-08-04T11:00:00Z", "value": 4}]


def test_history_without_a_source_retention_is_unavailable():
    entity = Entity(kind=Kind.COMPONENT, id="writer", state=State.HEALTHY,
                    provenance=Provenance(source="fixture"))
    assert query(entity, 24) == {"available": False, "reason": "source declares no retained history"}
