"""nousergon-console#60 closes-when: `Incidents.py`'s consolidated view must
either be reproduced by the generated Incident-kind index or built as a
bespoke pane, with the choice stated in the closing PR.

This asserts the generated index choice is correct: entities from all three
migrated sources (event-lake, quarantine, retro feed) land under one Kind and
are addressable at the single generated `/incident` list route with no
per-source code in the render layer — the same mechanism `Incidents.py`
implemented by hand as a lens-switcher over three Streamlit pages.
"""
from __future__ import annotations

from console.adapters import changelog_events, changelog_retro_feed
from console.index.graph import Index
from console.model.kinds import Kind
from console.server.router import path_for_list

from .test_changelog_events import (
    BUCKET as EVENTS_BUCKET,
    ENTRY_BODIES,
    QUARANTINE_BODIES,
    NOW,
    _entries_cfg,
    _quarantine_cfg,
    _lister,
    _reader,
)
from .test_changelog_retro_feed import BUCKET as RETRO_BUCKET, FEED, KEY as RETRO_KEY


def test_all_three_sources_land_under_one_incident_kind_index():
    idx = Index()
    idx.add_result(changelog_events.fetch(
        _entries_cfg(), lister=_lister(ENTRY_BODIES), reader=_reader(ENTRY_BODIES), now=NOW,
    ))
    idx.add_result(changelog_events.fetch(
        _quarantine_cfg(), lister=_lister(QUARANTINE_BODIES), reader=_reader(QUARANTINE_BODIES), now=NOW,
    ))
    idx.add_result(changelog_retro_feed.fetch(
        {"bucket": RETRO_BUCKET, "key": RETRO_KEY},
        reader=lambda b, k: dict(FEED), now=NOW,
    ))

    incidents = idx.of_kind(Kind.INCIDENT)
    ids = {e.id for e in incidents}

    # Event-lake entries.
    assert "evt-critical-1" in ids
    assert "evt-info-1" in ids
    # Quarantine reject.
    assert "2026-08-09/evt-quarantined-1" in ids
    # Retro-feed groups.
    assert "preopen-sf|preopen SF wedged on CFN stack" in ids
    assert "scanner|universe gate mismatch" in ids

    # One route, generated — no bespoke Incidents.py pane or route registered.
    assert path_for_list(Kind.INCIDENT) == "/incident"
