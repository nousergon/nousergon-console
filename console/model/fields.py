"""Declared fields — rendering data the console has never seen (§5.8).

A generic surface over a heterogeneous fleet must render a module's own numbers
**without knowing anything about that module**. There are exactly two ways to
get there: a renderer per domain, or **self-describing emission**. §5.8 and §2.6
both forbid the first, so this is the second.

Every field beyond the model's own carries a descriptor — `type`, `unit`,
`baseline` (or an explicit declaration of none), and one `render` hint from a
**closed** vocabulary. The console renders any declared field generically from
its descriptor, and nothing anywhere is keyed on a component id, a repo or a
domain.

Three rules do the work, and the third is the one that is easy to get wrong:

- **`unit` is required for a numeric field.** The fleet's canonical instance of
  getting this wrong: a column emitted as a normalized ratio and consumed as
  raw share volume silently failed 901 of 903 tickers for months
  (`observability-policy.md` §3.4).
- **`baseline: null` is a declaration**, and §5.4 then renders the number as
  telemetry — plain, uncoloured. Green means *better than the baseline*, and
  where there is no baseline there is no colour.
- **An undeclared field renders as opaque text and is counted, never dropped.**
  A dropped field is a fact the emitter believes is on the surface and is not —
  the worst of the three outcomes, because it fails silently on the *emitter's*
  side of a boundary they cannot see.

The render vocabulary is closed for the same reason §2.1's kinds are: an open
set becomes a plugin API, and a plugin API is a per-module rendering path with
a nicer name. Adding a hint is a PR against `console-policy.md` §5.8 (§12).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Mapping


class Render(enum.Enum):
    """The closed render-hint vocabulary (§5.8). Add by PR against the policy."""

    VALUE = "value"
    DURATION = "duration"
    BYTES = "bytes"
    RATIO = "ratio"
    COUNT = "count"
    TIMESERIES = "timeseries"
    LINK = "link"
    TEXT = "text"

    @classmethod
    def parse(cls, raw: object) -> "Render | None":
        for hint in cls:
            if hint.value == raw:
                return hint
        return None


#: Render hints whose values are numeric, and therefore need a unit.
NUMERIC_RENDERS: frozenset[Render] = frozenset(
    {Render.VALUE, Render.DURATION, Render.BYTES, Render.RATIO,
     Render.COUNT, Render.TIMESERIES}
)

#: A missing declaration is not an error and not a silent drop — it is its own
#: rendered outcome, counted so §5.8's "never dropped" is measurable.
UNDECLARED = "undeclared"


@dataclass(frozen=True)
class Field:
    """One declared field, ready to render without knowing who emitted it."""

    name: str
    value: Any
    unit: str | None = None
    baseline: Any = None
    baseline_declared: bool = False
    render: Render = Render.VALUE
    #: Why this field is not fully declared, or None when it is. Rendered on the
    #: field itself rather than logged: the emitter is the only one who can fix
    #: it, and they will never read the console's logs.
    defect: str | None = None

    @property
    def declared(self) -> bool:
        return self.defect is None

    @property
    def comparable(self) -> bool:
        """Whether §5.4 permits colouring this as a verdict.

        Only a field with an explicitly declared baseline is comparable. An
        absent baseline and a baseline of `null` are the same answer here and
        they are DIFFERENT facts to a reader, which is why both are rendered
        and neither is coloured.
        """
        return self.baseline_declared and self.baseline is not None


def parse(raw: Mapping[str, Any] | None) -> list[Field]:
    """Turn an emission's `fields` block into renderable fields.

    Never raises. An emitter's mistake must reach the surface as a visible
    defect on the field, not as an exception that removes the whole entity — a
    malformed descriptor is a fact about the emitter, and hiding it makes the
    emitter's own blind spot the console's.
    """
    if not isinstance(raw, Mapping):
        return []
    out: list[Field] = []
    for name in sorted(raw):
        out.append(_one(str(name), raw[name]))
    return out


def _one(name: str, spec: Any) -> Field:
    if not isinstance(spec, Mapping):
        # A bare value with no descriptor. Rendered opaque and counted (§5.8) —
        # never dropped, because the emitter believes it is on the surface.
        return Field(name=name, value=spec, render=Render.TEXT, defect=UNDECLARED)

    value = spec.get("value")
    hint = Render.parse(spec.get("render", "value"))
    if hint is None:
        return Field(
            name=name, value=value, render=Render.TEXT,
            defect=(
                f"render hint {spec.get('render')!r} is outside the closed "
                "vocabulary (§5.8) — rendered opaque rather than dropped"
            ),
        )

    unit = spec.get("unit")
    baseline_declared = "baseline" in spec
    baseline = spec.get("baseline")

    defect = None
    if hint in NUMERIC_RENDERS and _is_number(value) and not unit:
        # Not a warning and not a rejection: the field renders, and it renders
        # SAYING it has no unit, because a number whose unit is inferred from
        # context is the defect that failed 901 of 903 tickers.
        defect = "no unit declared — a number without a unit is not a measurement"

    return Field(
        name=name,
        value=value,
        unit=str(unit) if unit else None,
        baseline=baseline,
        baseline_declared=baseline_declared,
        render=hint,
        defect=defect,
    )


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def format_value(field: Field) -> str:
    """Render a declared value from its descriptor alone.

    Deliberately plain. This is the whole of the "renders what it has never
    seen" claim, and the moment it grows a branch on WHO emitted something it
    has become the per-module rendering path §5.8 forbids.
    """
    value = field.value
    if value is None:
        return "no value"
    if field.render is Render.DURATION and _is_number(value):
        return _duration(float(value))
    if field.render is Render.BYTES and _is_number(value):
        return _bytes(float(value))
    if field.render is Render.RATIO and _is_number(value):
        return f"{float(value):.4g}"
    if field.render is Render.COUNT and _is_number(value):
        return f"{int(value):,}"
    if field.render is Render.TIMESERIES and isinstance(value, (list, tuple)):
        return f"{len(value)} points"
    return str(value)


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.3g}s"
    if seconds < 3600:
        return f"{seconds / 60:.3g}m"
    return f"{seconds / 3600:.3g}h"


def _bytes(count: float) -> str:
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(count) < 1024 or suffix == "TiB":
            return f"{count:.3g} {suffix}"
        count /= 1024
    return f"{count:.3g} TiB"  # pragma: no cover - loop always returns
