"""Global search — the §3.7 identifier resolver.

One entry point resolving any identifier the fleet uses: a component id, run
id, cycle date, object key or prefix, tracker ref, clause id, incident id,
metric name, repo, PR number. Results are typed by kind and rank exact
identifier matches first.

Search is over the index, not over a page (§3.7) — it resolves against the
same entity ids the index holds, so a search that finds nothing is a true
negative (the identifier is not in the fleet), never a silently-excluded kind.
Every kind is in scope; an absent kind would be unreachable on path one of
three and its absence would be invisible.

A row's declared label (`detail["label"]`), and the name its source declares
for its job (`detail["job_label"]`, the name a folded run list shows), are
searched too, so a reader can
type the name a page shows them instead of the id behind it
(alpha-engine-config-I11805). Labels only ever ADD hits and always rank after
every identifier match: §3.7 forbids a search that matches display labels but
not identifiers, and an identifier pasted from a log line must still land on
its entity first.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..index.graph import Index
from ..model.entity import JOB_LABEL_DETAIL, LABEL_DETAIL, Entity

#: `Hit.matched_on` values: the identifier itself, or only the declared label.
MATCHED_ON_ID = "id"
MATCHED_ON_LABEL = "label"


@dataclass(frozen=True)
class Hit:
    entity: Entity
    exact: bool  # exact identifier match ranks first (§3.7)
    #: What the query matched. `label` means the id did not match and the
    #: declared label did, so the page can say why a row is there.
    matched_on: str = MATCHED_ON_ID


def _labels(ent: Entity) -> list[str]:
    """Every declared name of a row, lower-cased: its label, then its job's."""
    out = []
    for slot in (LABEL_DETAIL, JOB_LABEL_DETAIL):
        raw = ent.detail.get(slot)
        if isinstance(raw, str) and raw.strip():
            out.append(raw.strip().lower())
    return out


def search(index: Index, query: str) -> list[Hit]:
    """Resolve a query string to ranked hits over every kind.

    Ranking: exact id matches first, then id prefix matches, then id
    substring matches — so pasting a full identifier lands directly on the
    entity, and a partial identifier still surfaces candidates. After every
    identifier match come rows whose id did not match but whose declared
    label does: the label equal to the query, then starting with it, then
    containing every word of it in any order. Case is ignored throughout.
    All kinds are searched.
    """
    q = query.strip()
    if not q:
        return []
    ql = q.lower()
    words = ql.split()
    exact: list[Hit] = []
    prefix: list[Hit] = []
    substring: list[Hit] = []
    label_exact: list[Hit] = []
    label_prefix: list[Hit] = []
    label_words: list[Hit] = []
    for ent in index.all():
        eid = ent.id.lower()
        if eid == ql:
            exact.append(Hit(ent, exact=True))
        elif eid.startswith(ql):
            prefix.append(Hit(ent, exact=False))
        elif ql in eid:
            substring.append(Hit(ent, exact=False))
        else:
            labels = _labels(ent)
            if not labels:
                continue
            hit = Hit(ent, exact=False, matched_on=MATCHED_ON_LABEL)
            if any(label == ql for label in labels):
                label_exact.append(hit)
            elif any(label.startswith(ql) for label in labels):
                label_prefix.append(hit)
            elif any(all(w in label for w in words) for label in labels):
                label_words.append(hit)
    return exact + prefix + substring + label_exact + label_prefix + label_words
