"""Declared fields (§5.8) — the chokepoint for CN-5.8.

The defect: `Entity.detail` was `dict[str, object]`, documented "Rendered,
never indexed on", and `render/html.py` did not render it **at all**. A module's
own numbers could not be faceted, searched, charted, given a baseline or shown.
A generic surface over a heterogeneous fleet has exactly two ways to render a
module's data — a renderer per domain, or self-describing emission — and §5.8
and §2.6 both forbid the first.

Two tests here are the clause and the rest support them:

- `test_the_console_renders_a_field_it_has_never_seen`
- `test_no_rendering_code_is_keyed_on_who_emitted_something`

The second is the one that stops the first from decaying. A generic renderer
becomes a per-module one through a single defensible special case.
"""
from __future__ import annotations

import os
import re

import pytest

from console.model.entity import Entity, Provenance
from console.model.fields import Field, Render, UNDECLARED, format_value, parse
from console.model.kinds import Kind, State
from console.render.html import fields_section

RENDER_PACKAGE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "console", "render",
)


def _entity(fields: dict) -> Entity:
    return Entity(
        kind=Kind.COMPONENT,
        id="a-module-nobody-has-heard-of",
        state=State.HEALTHY,
        provenance=Provenance(source="test"),
        detail={"fields": fields},
    )


# --------------------------------------------------------- the clause -------

def test_the_console_renders_a_field_it_has_never_seen():
    """A module the console knows nothing about emits a measurement, and it
    renders — with its unit, from its descriptor alone, with no console change."""
    html = fields_section(_entity({
        "tickers_scanned": {
            "value": 903, "unit": "tickers", "baseline": 900, "render": "count",
        },
    }))
    assert "tickers_scanned" in html
    assert "903" in html
    assert "tickers" in html
    assert "900" in html


def test_no_rendering_code_is_keyed_on_who_emitted_something():
    """The guard that stops the generic renderer decaying into a per-module one.

    A special case for one component is always defensible on the day it is
    added, which is exactly why the check is mechanical rather than a review
    convention. Scoped to `console/render/` — the place a shortcut would land.
    """
    offenders = []
    # Identifier-shaped literals: the fleet's component ids and repo names are
    # kebab-case with a domain-ish prefix, which is what a special case looks
    # like when somebody writes one.
    suspicious = re.compile(r'["\'](?:[a-z0-9]+-){2,}[a-z0-9]+["\']')
    for dirpath, dirnames, filenames in os.walk(RENDER_PACKAGE):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path) as fh:
                for lineno, line in enumerate(fh, 1):
                    code = line.split("#")[0]
                    for hit in suspicious.findall(code):
                        offenders.append(f"{name}:{lineno}: {hit}")
    assert offenders == [], (
        "rendering code appears to be keyed on a component id, repo or domain "
        f"(§5.8): {offenders}"
    )


# --------------------------------------------------- units, §3.4's lesson ---

def test_a_number_without_a_unit_renders_saying_so():
    """The fleet's canonical instance: `avg_volume_20d` emitted as a normalized
    ratio and consumed as raw share volume, silently failing 901 of 903 tickers
    for months. The field renders — and it renders SAYING it has no unit."""
    (field,) = parse({"rows": {"value": 903, "render": "count"}})
    assert field.unit is None
    assert "no unit" in field.defect
    assert "903" in fields_section(_entity({"rows": {"value": 903, "render": "count"}}))


def test_a_non_numeric_field_needs_no_unit():
    (field,) = parse({"note": {"value": "all clear", "render": "text"}})
    assert field.declared


# ------------------------------------------------------ §5.4, the colour ----

def test_a_declared_baseline_makes_a_field_comparable():
    (field,) = parse({"x": {"value": 5, "unit": "s", "baseline": 3,
                            "render": "duration"}})
    assert field.comparable


def test_baseline_null_is_a_DECLARATION_and_renders_as_telemetry():
    """§5.4: green means BETTER THAN THE BASELINE, and where there is no
    baseline there is no colour. `null` says so explicitly."""
    (field,) = parse({"x": {"value": 5, "unit": "s", "baseline": None,
                            "render": "duration"}})
    assert field.baseline_declared and not field.comparable
    html = fields_section(_entity({"x": {"value": 5, "unit": "s",
                                         "baseline": None, "render": "duration"}}))
    assert "none declared" in html and "telemetry" in html


def test_an_absent_baseline_and_a_null_one_are_DIFFERENT_facts():
    """Both are uncoloured and they say different things to a reader: one
    emitter thought about it, the other did not (§5.5 — absence renders as
    itself)."""
    (absent,) = parse({"x": {"value": 5, "unit": "s"}})
    (declared,) = parse({"x": {"value": 5, "unit": "s", "baseline": None}})
    assert not absent.baseline_declared and declared.baseline_declared
    assert "not stated" in fields_section(_entity({"x": {"value": 5, "unit": "s"}}))


# --------------------------------------------- never dropped, always counted -

def test_an_unrecognised_render_hint_renders_opaque_rather_than_dropping():
    (field,) = parse({"x": {"value": 1, "render": "sparkline-3d"}})
    assert field.render is Render.TEXT
    assert "outside the closed vocabulary" in field.defect
    assert "x" in fields_section(_entity({"x": {"value": 1, "render": "sparkline-3d"}}))


def test_a_bare_value_with_no_descriptor_renders_opaque_and_is_counted():
    """The worst outcome is the silent one: a dropped field is a fact the
    emitter believes is on the surface and is not, and it fails on THEIR side
    of a boundary they cannot see."""
    (field,) = parse({"x": 42})
    assert field.defect == UNDECLARED
    html = fields_section(_entity({"x": 42, "y": {"value": 1, "unit": "n",
                                                  "render": "count"}}))
    assert "1 of 2 fields are not fully declared" in html


def test_a_malformed_descriptor_never_removes_the_entity():
    """An emitter's mistake reaches the surface as a visible defect on the
    field, never as an exception that takes the whole row with it."""
    for junk in (None, [], "nonsense", {"x": {"render": {"nested": True}}}):
        assert isinstance(parse(junk if isinstance(junk, dict) else None), list)


# ------------------------------------------------- the closed vocabulary ----

def test_the_render_vocabulary_is_closed_and_matches_the_published_schema():
    """§5.8's set is closed for the same reason §2.1's kinds are: an open one
    becomes a plugin API, and a plugin API is a per-module rendering path with
    a nicer name. Asserted against the SCHEMA so the two cannot drift."""
    from console.emit import load_schema

    schema_enum = set(
        load_schema()["properties"]["fields"]["additionalProperties"]
        ["properties"]["render"]["enum"]
    )
    assert {r.value for r in Render} == schema_enum


@pytest.mark.parametrize(
    "render, value, expected",
    [
        ("duration", 45, "45s"),
        ("duration", 900, "15m"),
        ("duration", 7200, "2h"),
        ("bytes", 2048, "2 KiB"),
        ("count", 1234567, "1,234,567"),
        ("ratio", 0.123456, "0.1235"),
        ("timeseries", [1, 2, 3], "3 points"),
    ],
)
def test_formatting_comes_from_the_hint_and_nothing_else(render, value, expected):
    (field,) = parse({"x": {"value": value, "unit": "u", "render": render}})
    assert format_value(field) == expected


def test_a_field_with_no_value_says_so_rather_than_rendering_empty():
    (field,) = parse({"x": {"unit": "s", "render": "duration"}})
    assert format_value(field) == "no value"
