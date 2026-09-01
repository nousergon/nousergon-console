"""The `records` source SHAPE — read once, used at two layers (§2.3).

A body's fan-out grammar — whole-body / list-of-dicts `records_path` /
grouped `*` fan-out with `group_field` / parallel `array_fields` / CSV rows —
plus per-record id minting (`id_template`), declared-field extraction
(`fields`, §5.8) and domain-state resolution (`state_field`/`state_map`,
`console-policy.md`'s "otherwise the value itself" convention) is ONE grammar
with two callers:

- `console/adapters/s3_records.py` (**adapter** direction): the console's own
  config names a whole prefix to enumerate; many keys, many bodies.
- `console/drivers/s3_records.py` (**driver** direction, `nousergon-console#98`):
  one descriptor names one document; one body.

Both callers read the identical grammar on the identical body shape, so this
module is the ONE place it is implemented. Forking it — writing the grammar a
second time inside the driver because it "is just a document read, not a
prefix scan" — is exactly the §2.3 defect this module's own PR history exists
to avoid (`nousergon-console#79`'s adapter consolidation, one layer up).

Everything here is pure: no I/O, no AWS, no config resolution. Both callers
own their own source access (listing a prefix vs. reading one declared
document) and pass this module a parsed JSON/CSV body plus the shape's own
declared knobs.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any

from .model.kinds import COMPONENT_STATE_KINDS, Kind, State


def get_path(obj: Any, path: str) -> Any:
    """Dotted-path lookup: `a.b.c` -> obj[a][b][c]. None on any missing hop."""
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


class RecordsSelectorError(ValueError):
    """A declared `limit`/`order` selector that cannot be honoured.

    A subclass of ValueError so both callers' existing "this body did not
    match its declared shape" handling still catches it, while a caller that
    wants to say *the SELECTOR is wrong, not the body* can tell them apart.
    """


def select(records: list[dict], limit: Any, order: Any) -> list[dict]:
    """Bound a fan-out to `limit` records from one declared end (§2.3).

    `alpha-engine-config-I9618`. Without this, a `records` source over a
    growing series mints one entity per record forever: `eod_pnl.csv` is 120
    trading sessions today and grows ~250/year, and every one of them would
    become a permanent console entity. A binding that cannot say *how many*
    and *which end* is not bindable at all.

    `order` is REQUIRED whenever `limit` is set, and has no default. Which end
    of a series a bounded window keeps is not inferable from the fact that it
    is bounded — a `limit: 30` that silently took the OLDEST 30 sessions would
    publish a number that is true about a window nobody asked for, which is
    the failure mode `console-policy.md`'s row contract exists to prevent. So
    it is declared, and its absence RAISES rather than picking one.

    - `order: "last"`  — the final `limit` records (a trailing window).
    - `order: "first"` — the leading `limit` records.

    Absent `limit`, the records pass through untouched: the unbounded fan-out
    stays the default for the bodies that are legitimately fixed-size (a
    report card's tiles, a leaderboard's arms).
    """
    if limit is None:
        if order is not None:
            raise RecordsSelectorError(
                f"`order: {order!r}` declared without a `limit` — an order on "
                f"an unbounded fan-out selects nothing and is a typo, not a "
                f"no-op"
            )
        return records
    try:
        n = int(limit)
    except (TypeError, ValueError):
        raise RecordsSelectorError(f"`limit` must be an integer, got {limit!r}") from None
    if n <= 0:
        raise RecordsSelectorError(
            f"`limit` must be a positive integer, got {n} — a binding that "
            f"declares zero records is a binding that should not be declared"
        )
    if order is None:
        raise RecordsSelectorError(
            "`limit` requires an explicit `order` of 'last' or 'first' — which "
            "end of the series a bounded window keeps is declared, never guessed"
        )
    if order not in ("last", "first"):
        raise RecordsSelectorError(
            f"`order` must be 'last' or 'first', got {order!r}"
        )
    return records[-n:] if order == "last" else records[:n]


def project(body: Any, fmt: str, records_path: str | None,
            array_fields: list[str] | None,
            group_field: str | None,
            limit: Any = None, order: Any = None) -> tuple[list[dict], dict]:
    """Turn one body into (records, body_root) per the declared shape.

    `limit`/`order` bound the fan-out — see `select`. They are applied HERE,
    inside the one grammar both the adapter and the driver read, so neither
    caller can end up with a different notion of what a bounded window is
    (§2.3). A caller that omits them gets the unbounded behaviour it had.

    ``body_root`` is what field paths resolve against for whole-body /
    body-level fields; each record is merged on top of it per-entity (record
    wins on key collision) so a per-record field and a body-level field are
    both reachable through the same ``path`` syntax.

    Raises `TypeError`/`ValueError`/`KeyError` on a body that does not match
    its declared shape — the caller's job to turn that into a per-key/per-
    binding finding rather than a crash that empties everything else it reads
    (§2.3).
    """
    if fmt == "csv":
        text = body if isinstance(body, str) else body.decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(text)))
        return select(rows, limit, order), {}

    parsed = json.loads(body) if isinstance(body, (str, bytes)) else body
    if not isinstance(parsed, dict):
        raise TypeError("records body must be a JSON object")

    if records_path:
        parts = str(records_path).split(".")
        if "*" in parts:
            return select(_explode_grouped(parsed, parts, group_field),
                          limit, order), parsed
        raw_list = get_path(parsed, records_path)
        if not isinstance(raw_list, list):
            raise TypeError(f"records_path {records_path!r} is not a list")
        return select([r for r in raw_list if isinstance(r, dict)],
                      limit, order), parsed
    if array_fields:
        arrays = [parsed.get(f) for f in array_fields]
        if any(not isinstance(a, list) for a in arrays):
            raise TypeError("array_fields must all name JSON arrays")
        rows = [
            {field: values[i] for field, values in zip(array_fields, arrays)}
            for i in range(min(len(a) for a in arrays))
        ]
        return select(rows, limit, order), parsed
    # Whole-body mode: the object IS the one record. A `limit` here is a
    # declaration about a fan-out that does not exist, so it still validates
    # (and still raises on a malformed selector) rather than being ignored.
    return select([{}], limit, order), parsed


def _explode_grouped(cur: Any, parts: list[str],
                      group_field: str | None) -> list[dict]:
    """Resolve a `records_path` containing a `*` segment. `*` iterates a
    dict's keys — rather than indexing a named key like every other segment —
    injecting each key under `group_field` into every record reached beneath
    it. The terminal value may be a list (each dict item becomes one record)
    or a dict (a dict-of-records: each VALUE becomes one record, its key
    injected the same way `*` injects one mid-path) — both are "a dict names
    several records" at different depths, so one walk covers both."""
    out: list[dict] = []
    _walk_grouped(cur, parts, group_field, out)
    return out


def _walk_grouped(cur: Any, parts: list[str], group_field: str | None,
                   out: list[dict]) -> None:
    if not parts:
        if isinstance(cur, dict):
            out.append(cur)
        elif isinstance(cur, list):
            out.extend(item for item in cur if isinstance(item, dict))
        return
    part, rest = parts[0], parts[1:]
    if part == "*":
        if not isinstance(cur, dict):
            return
        for key, value in cur.items():
            before = len(out)
            _walk_grouped(value, rest, group_field, out)
            for rec in out[before:]:
                rec.setdefault(group_field or "group", key)
        return
    if isinstance(cur, dict) and part in cur:
        _walk_grouped(cur[part], rest, group_field, out)


def flat_context(groups: dict[str, str], body_root: dict, record: dict) -> dict:
    """The scalar namespace `id_template`/`evidence_template` format against:
    key-pattern capture groups, then body-level scalars, then record scalars
    (record wins on collision — the same precedence `project`'s merged
    ``path_root`` uses for field lookups)."""
    return {
        **groups,
        **{k: v for k, v in body_root.items() if not isinstance(v, (dict, list))},
        **{k: v for k, v in record.items() if not isinstance(v, (dict, list))},
    }


def resolve_id(id_template: str, context: dict[str, Any]) -> str | None:
    """Mint one entity id from the declared template. None on a template
    naming a field this record/body does not carry, or an empty result —
    both render as "no entity for this record", never a crash."""
    if not id_template:
        return None
    try:
        entity_id = str(id_template).format(**context)
    except (KeyError, IndexError):
        return None
    return entity_id or None


def build_fields(path_root: dict, fields_config: dict[str, Any] | None,
                  question: str | None) -> dict[str, dict[str, Any]]:
    """Declared-field extraction (§5.8): `{field_name: {path, unit, baseline,
    render}}` resolved against the merged record+body structure. `question`
    (`console-policy.md` §4.4) is carried through as a synthetic `text`
    field, matching every other adapter/driver that reads this shape."""
    fields_out: dict[str, dict[str, Any]] = {}
    if question:
        fields_out["question"] = {"value": str(question), "render": "text"}
    for fname, spec in (fields_config or {}).items():
        # `path` defaults to the field's own name (nousergon-console#79): the
        # overwhelmingly common case is a field named after the key it reads.
        val = get_path(path_root, spec.get("path", fname))
        entry: dict[str, Any] = {"value": val, "render": spec.get("render", "value")}
        if spec.get("unit") is not None:
            entry["unit"] = spec["unit"]
        if "baseline" in spec:
            entry["baseline"] = spec["baseline"]
        fields_out[fname] = entry
    return fields_out


def resolve_state(
    kind: Kind,
    state_field: str | None,
    state_default: Any,
    state_map: dict[str, Any] | None,
    path_root: dict,
    as_of: str | None,
    cadence_seconds: float | None,
    staleness_factor: float,
    now: datetime,
) -> State | str:
    """§5.1: Component/Run resolve to one of the thirteen-state closed
    vocabulary; every other kind carries the source's own value verbatim.

    ``state_map`` translates the source's own vocabulary
    (``{"passed": "HEALTHY"}``) into the thirteen. The three not-computable
    outcomes stay three facts (§5.5): no value at all is `UNREPORTED`, a
    value nothing can interpret is `DEGRADED` (a finding, not a crash), and a
    `state_map` entry naming a state that does not exist is also `DEGRADED`
    — a typo in the map must never read as healthy.
    """
    raw = get_path(path_root, state_field) if state_field else None
    if raw is None:
        raw = state_default

    if kind in COMPONENT_STATE_KINDS:
        if raw is None:
            return State.UNREPORTED
        mapped = (state_map or {}).get(str(raw))
        if mapped is not None:
            try:
                return State[str(mapped).upper()]
            except KeyError:
                return State.DEGRADED
        try:
            return State[str(raw).upper()]
        except KeyError:
            return State.DEGRADED

    if raw is not None:
        return str(raw)

    # No domain state declared — freshness relative to a declared cadence
    # renders the value itself (§5.1's second half); the three not-computable
    # cases stay three facts (§5.5) rather than collapsing into one token.
    if as_of is None:
        return "no-freshness-stamp"
    if cadence_seconds is None:
        return "no-cadence-declared"
    try:
        ts = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
    except ValueError:
        return "unreadable"
    age = (now - ts).total_seconds()
    return "fresh" if age <= cadence_seconds * staleness_factor else "stale"
