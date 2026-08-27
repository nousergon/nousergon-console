"""One reader for the source shape "a YAML document naming many entities"
(`console-policy.md` §2.3, §2.5).

Two readers of the fleet's `ARTIFACT_REGISTRY.yaml` now exist and they are
halves of one row:

- `adapters/declared_registry.py` — the DECLARATION half: what is expected to
  exist, keyed by the document's own id field.
- `adapters/object_store.py`'s `keys_from:` binding — the OBSERVATION half: a
  HEAD per declared key, emitting a claim under the SAME identifier.

**Why this is one module rather than two private copies.** The merge in §2.5
is by identifier, so the two halves meet only while they agree, exactly, about
how the document is walked (`entries_field`), which of the two top-level
shapes it is in, and which field is the id. A second copy of that walk is not
a duplication of convenience — the first time the copies disagree by one
character the halves stop merging and the surface renders a declared row that
nothing observed BESIDE an observed row nothing declared, which is the defect
class this whole slice exists to remove, wearing a new hat
(`shared-code-policy.md`'s second-adoption trigger, reached here for a reason
stronger than repetition).

Nothing in here knows any deployment's field names, bucket, keys or paths: the
caller passes the field names its own config declares (§2.3).
"""
from __future__ import annotations

from typing import Any, Mapping

import yaml


def load(path: str) -> Any:
    """The parsed document. Raises — the caller decides what a source it
    cannot read renders as, and for both callers that is `FAILED`, never an
    empty result rendered as a fleet with no artifacts (§5.5)."""
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def dig(raw: Any, dotted: str | None) -> Any:
    """Walk a dotted path (`observations.entries`) into a nested document.

    A path that does not resolve returns `[]` rather than raising: a document
    whose shape does not match the configured field names has no entries to
    offer, which the caller reports as such.
    """
    if not dotted:
        return raw
    cursor = raw
    for part in str(dotted).split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            return []
        cursor = cursor[part]
    return cursor


def entries(raw: Any) -> list[tuple[str | None, Mapping[str, Any]]]:
    """`[(mapping-key-or-None, entry)]`. Both natural top-level shapes are
    accepted (mirrors `model/descriptor.py`'s "one mapping or a list of them" —
    a format that rejects a defensible spelling is a format people work
    around):

    - a list of entry mappings, each carrying its own id
    - a mapping of id -> entry, the mapping key supplying `entry_id`'s fallback
    """
    if isinstance(raw, list):
        return [(None, e) for e in raw if isinstance(e, Mapping)]
    if isinstance(raw, Mapping):
        return [(str(k), v) for k, v in raw.items() if isinstance(v, Mapping)]
    return []


def entry_id(
    entry: Mapping[str, Any], mapping_key: str | None, id_field: str,
) -> str | None:
    val = entry.get(id_field)
    if val:
        return str(val)
    if mapping_key is not None:
        return str(mapping_key)
    return None


def read(
    path: str, entries_field: str | None, id_field: str,
) -> list[tuple[str, Mapping[str, Any]]]:
    """`[(id, entry)]` — the whole walk in one call, for a caller that needs
    only identified entries. Entries with no resolvable id are dropped; a
    caller that must COUNT them walks the three steps itself."""
    raw = dig(load(path), entries_field)
    out: list[tuple[str, Mapping[str, Any]]] = []
    for mapping_key, entry in entries(raw):
        eid = entry_id(entry, mapping_key, id_field)
        if eid:
            out.append((eid, entry))
    return out
