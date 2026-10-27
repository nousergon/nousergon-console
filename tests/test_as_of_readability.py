"""A row's as-of reads at a glance: UTC to the minute and its age (I11805).

Before this, every as-of cell printed the source's raw stamp, so telling a
fresh row from a week-old one meant date arithmetic per row. The stamp as the
source wrote it stays in the `<time>` attributes and in the JSON (§3.8).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.render import json as render_json
from console.render.html import as_of_html
from console.server.app import _html
from console.server.router import resolve

NOW = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw, shown, age", [
    ("2026-10-27T09:59:30Z", "2026-10-27 09:59Z", "under a minute ago"),
    ("2026-10-27T09:18:00Z", "2026-10-27 09:18Z", "42m ago"),
    ("2026-10-27T03:00:00+00:00", "2026-10-27 03:00Z", "7h ago"),
    ("2026-10-25T11:00:00Z", "2026-10-25 11:00Z", "47h ago"),
    ("2026-10-20T10:00:00Z", "2026-10-20 10:00Z", "7d ago"),
    # An offset is converted to UTC, not shown as wall-clock time elsewhere.
    ("2026-10-27T04:30:00-04:00", "2026-10-27 08:30Z", "1h ago"),
    # Sub-second precision is dropped from the visible stamp only.
    ("2026-10-27T03:55:12.481203+00:00", "2026-10-27 03:55Z", "6h ago"),
])
def test_an_instant_reads_as_utc_minute_and_age(raw, shown, age):
    out = as_of_html(raw, NOW)
    assert f'<time datetime="{raw}" title="{raw}">' in out
    assert f'{shown} · <span class="age">{age}</span></time>' in out


def test_a_stamp_after_the_clock_says_so_rather_than_a_negative_age():
    out = as_of_html("2026-10-27T12:00:00Z", NOW)
    assert '<span class="age">2h ahead of now</span>' in out
    # Ordinary skew of under a minute is not called out.
    assert "under a minute ago" in as_of_html("2026-10-27T10:00:30Z", NOW)


@pytest.mark.parametrize("raw", [
    "2026-10-27",               # a date, not an instant
    "2026-10-27T03:00:00",      # no offset: converting would guess a timezone
    "yesterday-ish",            # unparseable
])
def test_a_stamp_without_an_offset_renders_verbatim(raw):
    assert as_of_html(raw, NOW) == raw


def test_a_verbatim_stamp_is_still_escaped():
    assert as_of_html("<b>x</b>", NOW) == "&lt;b&gt;x&lt;/b&gt;"


@pytest.mark.parametrize("raw", [None, ""])
def test_no_stamp_is_still_the_declared_absence(raw):
    assert as_of_html(raw, NOW) == '<em class="absent">no freshness stamp</em>'


def _index(entities) -> Index:
    index = Index()
    index.add_result(AdapterResult(
        name="f", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=tuple(entities),
    ))
    index.finalize()
    return index


def _component(cid: str, as_of: str | None) -> Entity:
    return Entity(kind=Kind.COMPONENT, id=cid, state=State.HEALTHY,
                  provenance=Provenance(source="fixture", as_of=as_of))


def test_list_rows_read_the_age_against_the_render_clock(monkeypatch):
    monkeypatch.setattr(render_html, "render_now", lambda: NOW)
    index = _index([_component("comp-a", "2026-10-27T07:00:00Z"),
                    _component("comp-b", None)])
    page = _html(index, resolve("/component", ""))
    assert ('<td><time datetime="2026-10-27T07:00:00Z" title="2026-10-27T07:00:00Z">'
            '2026-10-27 07:00Z · <span class="age">3h ago</span></time></td>') in page
    assert '<em class="absent">no freshness stamp</em>' in page


def test_entity_page_row_reads_the_same_way_and_json_keeps_the_raw_stamp(monkeypatch):
    monkeypatch.setattr(render_html, "render_now", lambda: NOW)
    raw = "2026-10-27T03:55:12.481203+00:00"
    index = _index([_component("comp-a", raw)])
    page = _html(index, resolve("/component/comp-a", ""))
    assert '2026-10-27 03:55Z · <span class="age">6h ago</span>' in page
    text = render_json.dumps(render_json.payload(index, resolve("/component/comp-a", "")))
    assert raw in text  # §3.8: the representation carries the source's stamp
    assert "6h ago" not in text  # the age is presentation, not a new fact
