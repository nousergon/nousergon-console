"""Retention-bounded temporal projections supplied by the entity's source."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def query(entity, requested_hours: int, now: datetime | None = None) -> dict[str, Any]:
    """Return only source-retained observations; never relabel a short window."""
    retention = entity.detail.get("history_retention_hours")
    if not isinstance(retention, (int, float)) or retention <= 0:
        return {"available": False, "reason": "source declares no retained history"}
    effective = min(requested_hours, int(retention))
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=effective)
    points = []
    for point in entity.detail.get("history") or []:
        if not isinstance(point, dict) or not point.get("as_of"):
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
