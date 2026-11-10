"""Retention-bounded temporal projections supplied by the entity's source."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import cast

from .model.entity import Entity


def query(entity: Entity, requested_hours: int, now: datetime | None = None) -> dict[str, object]:
    """Return only source-retained observations; never relabel a short window."""
    retention = entity.detail.get("history_retention_hours")
    if not isinstance(retention, (int, float)) or retention <= 0:
        return {"available": False, "reason": "source declares no retained history"}
    effective = min(requested_hours, int(retention))
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=effective)
    points: list[dict[object, object]] = []
    # Source detail is free-form. Keep iteration and dict validation at runtime.
    history = cast(Iterable[object], entity.detail.get("history") or [])
    for raw_point in history:
        if not isinstance(raw_point, dict):
            continue
        # isinstance establishes dict, but cannot determine its key/value types.
        point = cast(dict[object, object], raw_point)
        if not point.get("as_of"):
            continue
        try:
            stamp = datetime.fromisoformat(str(point["as_of"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp >= cutoff:
            points.append(dict(point))
    return {
        "available": True,
        "requested_hours": requested_hours,
        "effective_hours": effective,
        "retention_hours": int(retention),
        "bounded": effective != requested_hours,
        "points": sorted(points, key=lambda point: str(point["as_of"])),
    }
