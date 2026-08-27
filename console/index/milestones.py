"""Declared milestone predicates — §4.4's question, answered from config alone.

The question this pane answers, and no other pane does: **"has the declared
milestone been met, and which clause is holding it?"**

The landing view (§4.3) answers *"is anything wrong right now"*. That is a
different question from *"is the thing we said we were building finished"*, and
nothing on the surface answered the second: a milestone whose exit criteria are
five measurable clauses was being evaluated by hand, from a tracker issue, by a
human reading five other surfaces. A predicate re-derived by hand is a second
inventory (§2.4) in the one place it hurts most — the place where somebody
decides a phase of work is over.

**Nothing about any particular milestone lives in this repo.** A milestone is a
config declaration: an id, the question sentence, a tracker link, and a list of
clauses, each binding to a fact the index *already renders*. Two binding kinds,
because those are the two shapes a fact on this surface takes:

``number``
    a key of the §9 numbers dict (`render/json.py::numbers`), with `path`
    selecting a member of that number's own shape. Its as-of is the index's
    build time, because a §9 number is computed from this build and is exactly
    as old as it is.

``entity``
    an entity id on the index and a `field` on it — a declared field
    (`detail["fields"][<name>]["value"]`, §5.8) or a plain `detail` key. Its
    as-of, source and evidence are the entity's own, per field (§5.1).

Three things this deliberately does NOT do:

- **No aggregate green light** (§4.3). The roll-up is `N of M clauses met` plus
  the ids of the clauses holding it, never one boolean and never one colour. A
  milestone whose fifth clause cannot be read is not four-fifths met, it is
  four met and one unreadable, and those are different facts.
- **No clause renders blank.** A binding that resolves to no entity, no field,
  no number, a `computable: False` refusal, or a value that cannot be compared
  with its target renders ``UNREPORTED`` with the reason (§5.5, §8.3's
  totality). UNREPORTED is never met and never quietly unmet — an unmet clause
  says "measured, and it fails", which is a claim this pane has not earned when
  it could not read the input.
- **No evaluation is cached.** Like `reachability()` and `conflicts()`, this is
  a pure function of an already-built index plus its declarations, recomputed
  per query. The console persists nothing (§5.6).

Declarations are ATTACHED to the index rather than stored on it, via the
module-level `attach`/`declared` pair below. `Index` is the entity graph; a
milestone declaration is configuration, and putting a config list on the graph
type would make every future config concern a candidate for the same. The
accessor pair keeps all milestone knowledge in this one module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..model.fields import parse as parse_fields

#: The closed comparator vocabulary. Closed for the same reason §5.8's render
#: hints are: an open set is a plugin API, and a comparator this module does
#: not implement must fail the BUILD naming the fragment, never evaluate to
#: something plausible at render time.
COMPARATORS: frozenset[str] = frozenset({"==", "!=", ">=", "<=", ">", "<"})

#: Comparators that require both sides to be numbers. `==`/`!=` are defined
#: over any two values; the orderings are not, and comparing a string to an int
#: with `>=` is a config defect, not a False.
_ORDERING = frozenset({">=", "<=", ">", "<"})

#: The two binding kinds. A third is a PR against `console-policy.md`, exactly
#: as an eighth entity kind is.
BINDINGS: frozenset[str] = frozenset({"number", "entity"})

MET = "MET"
UNMET = "UNMET"
UNREPORTED = "UNREPORTED"

#: Where the declarations hang. See the module docstring: configuration, not
#: graph.
_ATTR = "_console_milestone_declarations"


class MilestoneConfigError(ValueError):
    """A milestone declaration this build cannot evaluate.

    Raised by `parse` during `config.build_index`, so an unknown binding kind
    or comparator fails the BUILD — which is the same gate `console index
    --config config.example.yaml` runs on every PR (§3.6's build-time
    posture). The alternative, degrading at render time, publishes a pane that
    silently answers a question nobody asked it.

    Every message names the milestone id and, where there is one, the clause
    id — the two identifiers that locate the declaring config fragment, since
    the assembled `config.yaml` the console reads has no memory of which file
    contributed which block.
    """


# ------------------------------------------------------------- declaration --

@dataclass(frozen=True)
class Term:
    """One comparison against one bound fact."""

    binding: str          # "number" | "entity"
    ref: str              # a §9 number key, or an entity id
    selector: str         # `path` for a number, `field` for an entity
    op: str
    target: Any

    @property
    def label(self) -> str:
        return f"{self.binding}:{self.ref}.{self.selector}"


@dataclass(frozen=True)
class Requirement:
    """A precondition on a clause's own binding.

    The canonical case is a §9 number that refuses to render — `staleness_honesty`
    publishes `computable: False` with a reason rather than a count over an
    unestablished population (§5.3). A clause bound to its `count` must then be
    UNREPORTED, not `0 == 0` and met. `requires: {path: computable, equals: true}`
    is that stated in config, so the console needs no knowledge of which numbers
    can refuse.
    """

    binding: str
    ref: str
    selector: str
    equals: Any


@dataclass(frozen=True)
class Clause:
    id: str
    label: str
    terms: tuple[Term, ...]
    requires: tuple[Requirement, ...]


@dataclass(frozen=True)
class Milestone:
    id: str
    question: str
    tracker: str | None
    clauses: tuple[Clause, ...]


# ------------------------------------------------------------------ parse --

def parse(raw: Any) -> tuple[Milestone, ...]:
    """Validate and freeze the `milestones:` config block. Raises on anything
    it could not evaluate honestly later."""
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise MilestoneConfigError(
            "`milestones:` must be a list of milestone declarations, not "
            f"{type(raw).__name__}"
        )
    out: list[Milestone] = []
    seen: set[str] = set()
    for ordinal, entry in enumerate(raw, start=1):
        if not isinstance(entry, Mapping):
            raise MilestoneConfigError(
                f"milestone #{ordinal}: each entry must be a mapping, not "
                f"{type(entry).__name__}"
            )
        mid = str(entry.get("id") or "").strip()
        if not mid:
            raise MilestoneConfigError(
                f"milestone #{ordinal}: `id` is required — it is the milestone's "
                "identity on the surface and in the JSON"
            )
        if mid in seen:
            raise MilestoneConfigError(
                f"milestone {mid!r}: declared twice — one namespace, one name "
                "(§3.6)"
            )
        seen.add(mid)
        question = str(entry.get("question") or "").strip()
        if not question:
            raise MilestoneConfigError(
                f"milestone {mid!r}: `question` is required — §4.4 requires the "
                "sentence the pane answers to be RENDERED on the pane, so it "
                "cannot be left to a reviewer's memory"
            )
        clauses = tuple(
            _clause(mid, ordinal2, c)
            for ordinal2, c in enumerate(entry.get("clauses") or (), start=1)
        )
        if not clauses:
            raise MilestoneConfigError(
                f"milestone {mid!r}: declares no clauses — a predicate with no "
                "terms is answered by nothing and would render as vacuously met"
            )
        tracker = entry.get("tracker")
        out.append(Milestone(
            id=mid, question=question,
            tracker=str(tracker) if tracker else None,
            clauses=clauses,
        ))
    return tuple(out)


def _clause(milestone_id: str, ordinal: int, raw: Any) -> Clause:
    where = f"milestone {milestone_id!r} clause #{ordinal}"
    if not isinstance(raw, Mapping):
        raise MilestoneConfigError(
            f"{where}: each clause must be a mapping, not {type(raw).__name__}"
        )
    cid = str(raw.get("id") or "").strip()
    if not cid:
        raise MilestoneConfigError(f"{where}: `id` is required")
    where = f"milestone {milestone_id!r} clause {cid!r}"
    label = str(raw.get("label") or cid)

    inline = "all_of" not in raw
    if inline:
        terms = (_term(where, raw),)
    else:
        all_of = raw.get("all_of")
        if not isinstance(all_of, Sequence) or isinstance(all_of, (str, bytes)) \
                or not all_of:
            raise MilestoneConfigError(
                f"{where}: `all_of` must be a non-empty list of bindings"
            )
        terms = tuple(_term(where, t) for t in all_of)

    requires = tuple(
        _requirement(where, r, terms[0] if inline else None)
        for r in _as_list(raw.get("requires"))
    )
    return Clause(id=cid, label=label, terms=terms, requires=requires)


def _as_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        return [raw]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return list(raw)
    raise MilestoneConfigError(
        f"`requires` must be a mapping or a list of mappings, not "
        f"{type(raw).__name__}"
    )


def _binding_of(where: str, raw: Mapping[str, Any]) -> tuple[str, str, str]:
    """The (binding kind, ref, selector) triple, validated.

    An unknown key where `number:`/`entity:` belongs is the failure this
    catches first: it is indistinguishable, at render time, from a binding that
    resolved to nothing — and one is a config typo while the other is a real
    UNREPORTED finding about the fleet.
    """
    declared = [k for k in BINDINGS if k in raw]
    if len(declared) != 1:
        raise MilestoneConfigError(
            f"{where}: exactly one of {sorted(BINDINGS)} must be declared, "
            f"found {sorted(declared) or 'none'} — an unknown binding kind "
            "cannot be evaluated and must not render as an absence"
        )
    binding = declared[0]
    ref = str(raw[binding] or "").strip()
    if not ref:
        raise MilestoneConfigError(f"{where}: `{binding}` names nothing")
    selector_key = "path" if binding == "number" else "field"
    selector = raw.get(selector_key)
    if selector is None or not str(selector).strip():
        raise MilestoneConfigError(
            f"{where}: a `{binding}` binding requires `{selector_key}` — "
            "which member of it is being compared"
        )
    return binding, ref, str(selector).strip()


def _term(where: str, raw: Any) -> Term:
    if not isinstance(raw, Mapping):
        raise MilestoneConfigError(
            f"{where}: each binding must be a mapping, not {type(raw).__name__}"
        )
    binding, ref, selector = _binding_of(where, raw)
    op = str(raw.get("op") or "").strip()
    if op not in COMPARATORS:
        raise MilestoneConfigError(
            f"{where}: comparator {op!r} is outside the closed vocabulary "
            f"{sorted(COMPARATORS)}"
        )
    if "target" not in raw:
        raise MilestoneConfigError(
            f"{where}: `target` is required — a value with no target is "
            "telemetry, never a verdict (§5.4)"
        )
    return Term(binding=binding, ref=ref, selector=selector, op=op,
                target=raw["target"])


def _requirement(where: str, raw: Any, inherit: Term | None) -> Requirement:
    if not isinstance(raw, Mapping):
        raise MilestoneConfigError(
            f"{where}: each `requires` entry must be a mapping, not "
            f"{type(raw).__name__}"
        )
    if "equals" not in raw:
        raise MilestoneConfigError(
            f"{where}: a `requires` entry needs `equals` — the value the "
            "precondition must hold for the clause to be evaluable"
        )
    if any(k in raw for k in BINDINGS):
        binding, ref, selector = _binding_of(where, raw)
    elif inherit is not None:
        selector_key = "path" if inherit.binding == "number" else "field"
        selector = raw.get(selector_key)
        if selector is None or not str(selector).strip():
            raise MilestoneConfigError(
                f"{where}: a `requires` entry inheriting this clause's "
                f"`{inherit.binding}` binding still needs `{selector_key}`"
            )
        binding, ref, selector = inherit.binding, inherit.ref, str(selector).strip()
    else:
        raise MilestoneConfigError(
            f"{where}: a `requires` entry on an `all_of` clause must name its "
            f"own binding (one of {sorted(BINDINGS)}) — there is no single "
            "binding for it to inherit"
        )
    return Requirement(binding=binding, ref=ref, selector=selector,
                       equals=raw["equals"])


# ----------------------------------------------------------- attachment ----

def attach(index: Any, milestones: Iterable[Milestone]) -> None:
    """Hang the parsed declarations off a built index (see module docstring)."""
    setattr(index, _ATTR, tuple(milestones))


def declared(index: Any) -> tuple[Milestone, ...]:
    """The declarations attached to this index, or none.

    An index built directly via `Index()` — every test that does not go through
    `config.build_index` — has none, and the pane then renders nothing at all
    rather than an empty predicate. A milestone nobody declared is not a
    milestone that failed.
    """
    return tuple(getattr(index, _ATTR, ()))


# ------------------------------------------------------------- evaluation --

@dataclass(frozen=True)
class Resolution:
    """One term's bound fact, or the reason it has none."""

    value: Any
    as_of: str | None
    source: str
    evidence: str | None
    reason: str | None = None   # set iff the fact could not be read

    @property
    def reported(self) -> bool:
        return self.reason is None


def evaluate(index: Any, numbers: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every declared milestone, evaluated over this build.

    `numbers` is the already-assembled §9 dict — passed in rather than
    recomputed, so the value a clause is graded on is byte-identical to the
    value rendered two sections further down the same page (§3.8: one query,
    two representations, and here one computation behind both).
    """
    return [_milestone(index, numbers, m) for m in declared(index)]


def _milestone(index: Any, numbers: Mapping[str, Any],
               milestone: Milestone) -> dict[str, Any]:
    clauses = [_clause_result(index, numbers, c) for c in milestone.clauses]
    statuses = [c["status"] for c in clauses]
    return {
        "id": milestone.id,
        "question": milestone.question,
        "tracker": milestone.tracker,
        # §5.3: the count carries its population inline. No boolean, no colour,
        # no single dot — §4.3 forbids the aggregate green light and a milestone
        # is the most tempting place to add one.
        "met": statuses.count(MET),
        "of": len(clauses),
        "unreported": statuses.count(UNREPORTED),
        # The half of the question the count cannot answer: WHICH clause is
        # holding it. Ordered as declared, so the answer is stable between
        # builds.
        "holding": [c["id"] for c in clauses if c["status"] != MET],
        "clauses": clauses,
    }


def _clause_result(index: Any, numbers: Mapping[str, Any],
                   clause: Clause) -> dict[str, Any]:
    for requirement in clause.requires:
        res = _resolve(index, numbers, requirement.binding, requirement.ref,
                       requirement.selector)
        if not res.reported:
            return _clause_doc(clause, UNREPORTED, [], res.reason)
        if res.value != requirement.equals:
            return _clause_doc(
                clause, UNREPORTED, [],
                f"precondition {requirement.binding}:{requirement.ref}."
                f"{requirement.selector} is {res.value!r}, not "
                f"{requirement.equals!r} — the bound fact declined to state a "
                "value, so this clause is unmeasured rather than unmet",
            )

    terms: list[dict[str, Any]] = []
    unreported: list[str] = []
    for term in clause.terms:
        res = _resolve(index, numbers, term.binding, term.ref, term.selector)
        if not res.reported:
            terms.append(_term_doc(term, res, UNREPORTED))
            unreported.append(f"{term.label}: {res.reason}")
            continue
        verdict = compare(res.value, term.op, term.target)
        if verdict is None:
            reason = (
                f"{_render(res.value)} cannot be compared to "
                f"{_render(term.target)} with {term.op} — an ordering over "
                "values that are not both numbers is a config defect, not a "
                "failed clause"
            )
            terms.append(_term_doc(term, res, UNREPORTED, reason))
            unreported.append(f"{term.label}: {reason}")
            continue
        terms.append(_term_doc(term, res, MET if verdict else UNMET))

    if unreported:
        return _clause_doc(clause, UNREPORTED, terms, "; ".join(unreported))
    status = MET if all(t["status"] == MET for t in terms) else UNMET
    return _clause_doc(clause, status, terms, None)


def _clause_doc(clause: Clause, status: str, terms: list[dict[str, Any]],
                reason: str | None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": clause.id,
        "label": clause.label,
        "status": status,
        "terms": terms,
    }
    if reason:
        doc["reason"] = reason
    return doc


def _term_doc(term: Term, res: Resolution, status: str,
              reason: str | None = None) -> dict[str, Any]:
    """One term on the wire — §5.1's four fields, plus what it was measured
    against. A row that cannot say how it knows is not yet trustworthy, and a
    predicate row must additionally say what it would take to be met."""
    doc: dict[str, Any] = {
        "binding": term.binding,
        "ref": term.ref,
        "selector": term.selector,
        "op": term.op,
        "target": term.target,
        "value": res.value,
        "status": status,
        "source": res.source,
        "as_of": res.as_of,
        "evidence": res.evidence,
    }
    if reason or res.reason:
        doc["reason"] = reason or res.reason
    return doc


def compare(value: Any, op: str, target: Any) -> bool | None:
    """The comparator, or `None` when these two values cannot be compared.

    `None` is not False. A clause whose value is `None` (the field exists and
    the producer wrote nothing) or whose ordering is over a non-number renders
    UNREPORTED — "we could not measure this" — rather than UNMET, which claims
    a measurement was made and failed.
    """
    if op == "==":
        return _eq(value, target)
    if op == "!=":
        eq = _eq(value, target)
        return None if eq is None else not eq
    if value is None or target is None:
        return None
    if not (_is_number(value) and _is_number(target)):
        return None
    if op == ">=":
        return value >= target
    if op == "<=":
        return value <= target
    if op == ">":
        return value > target
    return value < target  # op == "<", the vocabulary is closed and validated


def _eq(value: Any, target: Any) -> bool | None:
    if value is None and target is not None:
        # An absent value is not "not equal to PASS"; it is unmeasured.
        return None
    return value == target


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _render(value: Any) -> str:
    return "no value" if value is None else repr(value)


# ------------------------------------------------------------- resolution --

def _resolve(index: Any, numbers: Mapping[str, Any], binding: str, ref: str,
             selector: str) -> Resolution:
    if binding == "number":
        return _resolve_number(index, numbers, ref, selector)
    return _resolve_entity(index, ref, selector)


def _resolve_number(index: Any, numbers: Mapping[str, Any], ref: str,
                    selector: str) -> Resolution:
    """A §9 number's own member. As-of is the index build time: a §9 number is
    computed from this build and is exactly as old as the build is (§5.9)."""
    built_at = getattr(index, "build_info", None)
    as_of = getattr(built_at, "built_at", None) or None
    source = f"console index §9 number {ref}"
    if ref not in numbers:
        return Resolution(
            None, as_of, source, "/",
            reason=(f"no §9 number named {ref!r} on this build — the console "
                    f"publishes {sorted(numbers)}"),
        )
    doc = numbers[ref]
    value, found = _dig(doc, selector)
    if not found:
        return Resolution(
            None, as_of, source, "/",
            reason=(f"§9 number {ref!r} carries no member {selector!r} on this "
                    "build"),
        )
    return Resolution(value, as_of, source, "/")


def _resolve_entity(index: Any, ref: str, selector: str) -> Resolution:
    """An entity's declared field (§5.8) or a plain `detail` key.

    Declared fields are read through `model.fields.parse` rather than out of the
    raw mapping, so a field the emitter declared badly resolves to the same
    value the entity page renders — one reading, not two.
    """
    ent = index.entity(ref)
    if ent is None:
        return Resolution(
            None, None, f"entity {ref}", None,
            reason=(f"no entity {ref!r} on this index — its source has not "
                    "landed yet, or the identifier is wrong. `/doctor?q=" + ref
                    + "` says which"),
        )
    prov = ent.source_of(selector)
    as_of = prov.as_of or ent.provenance.as_of
    source = prov.source or ent.provenance.source
    evidence = _entity_route(ent)
    for field in parse_fields(ent.detail.get("fields")):
        if field.name == selector:
            return Resolution(field.value, as_of, source, evidence)
    if selector in ent.detail:
        return Resolution(ent.detail[selector], as_of, source, evidence)
    return Resolution(
        None, as_of, source, evidence,
        reason=(f"entity {ref!r} declares no field {selector!r} — it is on the "
                "surface, but the fact this clause binds to is not"),
    )


def _entity_route(ent: Any) -> str | None:
    from ..server.router import path_for_entity

    try:
        return path_for_entity(ent.kind, ent.id)
    except Exception:  # pragma: no cover - a kind outside the closed set
        return None


def _dig(doc: Any, path: str) -> tuple[Any, bool]:
    """Walk a dotted path into a §9 number's shape. Returns (value, found)."""
    cursor = doc
    for part in path.split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            return None, False
        cursor = cursor[part]
    return cursor, True
