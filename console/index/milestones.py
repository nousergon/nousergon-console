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

#: The milestone-level roll-up, in a CLOSED three-value vocabulary. It is not
#: the aggregate green light §4.3 forbids: `EXITED` is reachable ONLY when every
#: clause is MET, and `UNREPORTABLE` keeps §5.5's third state distinct at the
#: milestone level exactly as it is at the clause level. Collapsing UNREPORTED
#: into "not exited" would make an unreadable predicate indistinguishable from a
#: measured failing one, which is the distinction this module exists to hold.
EXITED = "EXITED"
HOLDING = "HOLDING"
UNREPORTABLE = "UNREPORTABLE"

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
class NotifySpec:
    """Where a clause transition is announced.

    Two legs, because `observability-policy.md` §7.3 says a human-only alert is
    invisible: the response plane cannot see it and nothing owns follow-through.
    `sns_topic_arn` is the operator leg; `event_bus` is the machine-readable one.
    Both are OPTIONAL and both are fleet topology, so both arrive as
    configuration — this repository is public and holds no ARN, bus name or
    account id of its own (§2.3).
    """

    sns_topic_arn: str | None = None
    event_bus: str | None = None
    event_source: str = "nousergon.console"
    region: str | None = None

    @property
    def declared(self) -> bool:
        return bool(self.sns_topic_arn or self.event_bus)


@dataclass(frozen=True)
class JournalSpec:
    """The durable home of a milestone's clause-state history.

    **The console renders; it never owns** (§5.6). The clause verdicts are the
    one fact on this surface that no other system produces — nothing else
    evaluates the declared predicate — so the console is their legitimate
    PRODUCER, and this is where it puts them so they outlive the process. What
    it does not do is keep them: every figure the pane renders about history is
    read back from this object, and a build that finds the object missing
    renders "no recorded history", never a reconstructed one.

    ``heartbeat_minutes`` exists because absence of a signal is never health
    (§5.5): a journal rewritten only on change is indistinguishable, by age,
    from a journal nothing is writing any more. The heartbeat re-stamps
    ``updated_at`` on a declared cadence so the object's own staleness is a
    readable fact. It writes no transition and notifies nothing.
    """

    bucket: str
    object_key: str
    retention_days: int = 400
    heartbeat_minutes: float = 60.0
    max_transitions: int = 500
    notify: NotifySpec = NotifySpec()


@dataclass(frozen=True)
class Milestone:
    id: str
    question: str
    tracker: str | None
    clauses: tuple[Clause, ...]
    journal: JournalSpec | None = None


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
            journal=_journal(mid, entry.get("journal")),
        ))
    return tuple(out)


def _journal(milestone_id: str, raw: Any) -> JournalSpec | None:
    """Validate the optional `journal:` block. Absent, the milestone is
    evaluated and rendered exactly as before and nothing is persisted — which
    is what `config.example.yaml` and every unit test do, so no build outside a
    deployment that asked for one ever writes an object."""
    if raw is None:
        return None
    where = f"milestone {milestone_id!r} `journal`"
    if not isinstance(raw, Mapping):
        raise MilestoneConfigError(
            f"{where}: must be a mapping, not {type(raw).__name__}"
        )
    bucket = str(raw.get("bucket") or "").strip()
    obj = str(raw.get("key") or "").strip()
    if not bucket or not obj:
        raise MilestoneConfigError(
            f"{where}: both `bucket` and `key` are required — a journal with "
            "nowhere to land would silently keep no history while the pane "
            "rendered a history section"
        )
    if obj.endswith("/"):
        raise MilestoneConfigError(
            f"{where}: `key` is one object, not a prefix — {obj!r} ends in '/'"
        )
    notify_raw = raw.get("notify") or {}
    if not isinstance(notify_raw, Mapping):
        raise MilestoneConfigError(
            f"{where}: `notify` must be a mapping, not "
            f"{type(notify_raw).__name__}"
        )
    notify = NotifySpec(
        sns_topic_arn=_opt_str(notify_raw.get("sns_topic_arn")),
        event_bus=_opt_str(notify_raw.get("event_bus")),
        event_source=str(notify_raw.get("event_source")
                         or "nousergon.console"),
        region=_opt_str(notify_raw.get("region")),
    )
    return JournalSpec(
        bucket=bucket, object_key=obj,
        retention_days=_positive(where, "retention_days",
                                 raw.get("retention_days"), 400),
        heartbeat_minutes=_positive(where, "heartbeat_minutes",
                                    raw.get("heartbeat_minutes"), 60.0),
        max_transitions=int(_positive(where, "max_transitions",
                                      raw.get("max_transitions"), 500)),
        notify=notify,
    )


def _opt_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _positive(where: str, field: str, value: Any, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or value <= 0:
        raise MilestoneConfigError(
            f"{where}: `{field}` must be a positive number, not {value!r}"
        )
    return float(value)


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


#: Where this build's journal outcome hangs — same reasoning as `_ATTR`: it is
#: a per-build report about configuration, not a node on the entity graph.
_REPORT_ATTR = "_console_milestone_journal_report"


def attach_journal_report(index: Any, reports: Iterable[Mapping[str, Any]]) -> None:
    """Record what the journal step did on this build, so the pane can render
    it. A journal that could not be written is a fact about the surface's own
    honesty and must appear on the surface — a recorder failing silently is the
    exact shape of defect this whole module exists to remove."""
    setattr(index, _REPORT_ATTR, tuple(dict(r) for r in reports))


def journal_report(index: Any) -> tuple[dict[str, Any], ...]:
    """This build's journal outcome, or none (an index built directly, or a
    deployment that declares no journal)."""
    return tuple(getattr(index, _REPORT_ATTR, ()))


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
    doc = {
        "id": milestone.id,
        "question": milestone.question,
        "tracker": milestone.tracker,
        # The whole predicate as ONE machine-readable field, so "has this
        # milestone exited" is answered by reading one value rather than by
        # comparing five clause rows and remembering which of the three
        # statuses each of them may take. Closed vocabulary, three values, and
        # EXITED is reachable only from all-MET (`_exit_state`).
        "exit_state": _exit_state(statuses),
        # The same fact as an integer, because the fleet's gate sweeps parse
        # only `field <name> >= <num>` (gate-taxonomy-policy.md §5's shared
        # grammar) — a string comparison is not in that grammar, so a predicate
        # written against `exit_state` would be UNCONSTRUCTIBLE and the sweep
        # would escalate it rather than evaluate it.
        "exit_confirmed": 1 if _exit_state(statuses) == EXITED else 0,
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
    if milestone.journal is not None:
        doc["journal"] = _uri(milestone.journal)
        # Rendered on the pane and served on the wire so the predicate a sweep
        # would evaluate is READ OFF THE SURFACE rather than transcribed into
        # an issue body by hand — a transcribed predicate is the one that ends
        # up with a trailing space inside the prefix and never evaluates.
        doc["verified_when"] = (
            f"{_uri(milestone.journal)} field exit_confirmed >= 1"
        )
    return doc


def _exit_state(statuses: Sequence[str]) -> str:
    """The milestone roll-up. Three values, and the ordering matters.

    `EXITED` requires EVERY clause MET — there is no path to it through an
    unread clause, which is what makes it safe to publish as one field.
    A single UNMET is `HOLDING`: something was measured and it fails.
    Otherwise (no UNMET, at least one UNREPORTED) the honest answer is
    `UNREPORTABLE` — we could not read the predicate, which is a different fact
    from "the predicate fails" and must never be rendered as either that or as
    a pending green.
    """
    if all(status == MET for status in statuses):
        return EXITED
    if any(status == UNMET for status in statuses):
        return HOLDING
    return UNREPORTABLE


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
    empty = _empty_population(doc, selector)
    if empty is not None:
        return Resolution(value, as_of, source, "/", reason=empty)
    return Resolution(value, as_of, source, "/")


#: Selectors that describe the DENOMINATOR or the number's own willingness to
#: answer, rather than a reading over it. The empty-population guard must not
#: fire on these or `requires: {path: computable, equals: true}` — the exact
#: mechanism that keeps §9.6's refusal honest — could never be evaluated.
_DENOMINATOR_SELECTORS = frozenset({"of", "computable", "state", "reason"})


def _empty_population(doc: Any, selector: str) -> str | None:
    """`{count: 0, of: 0}` REFUSES (alpha-engine-config-I9052).

    A fallback build renders every source `ok` over an empty population, and a
    gap count of `0 of 0` then reads as a perfect surface — which is how a
    blanked index looked healthy for ten minutes on 2026-08-28. A clause bound
    to such a count must be UNREPORTED: nothing was measured, so nothing may be
    graded, in either direction (§5.5's third state).

    Applies to any §9 number stating its denominator inline (§5.3) — which is
    all of them — and only when the denominator is present and not a positive
    number. `of: None` is §9.1's own signal for uncomputable and lands here too.
    """
    if selector in _DENOMINATOR_SELECTORS or not isinstance(doc, Mapping):
        return None
    if "of" not in doc:
        return None
    of = doc["of"]
    if _is_number(of) and of > 0:
        return None
    return (
        f"the population this number is measured over is {_render(of)} — a "
        "reading over an empty or uncomputable denominator is not evidence of "
        "health, and `0 of 0` is the shape a blanked index takes "
        "(alpha-engine-config-I9052)"
    )


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


# ---------------------------------------------------------------- journal --
#
# WHY ANY OF THIS EXISTS. Two clauses of the Crucible phase-2 exit predicate
# went MET -> UNMET inside three days (c3 `unregistered` 0 -> 3, c4
# `staleness_honesty` 0 -> 2, measured 2026-08-31T15:59Z) and NOTHING paged.
# The predicate was rendered on a page and read by a human when a human
# happened to look, which makes it a dashboard rather than a gate — and it is
# why the phase-2 clock kept resetting invisibly. The same regression was
# recorded once before, as a symptom, in `alpha-engine-config-I9083`; nothing
# was built that would catch the next one, so this is that.
#
# THREE THINGS IT IS CAREFUL ABOUT.
#
# * **The console renders; it never owns** (§5.6). The clause verdicts are the
#   one fact here that no other system produces, so the console is their
#   producer — but it does not KEEP them. They land in one declared S3 object
#   and every historical figure the pane shows is read back from it. A build
#   that finds no object renders "no recorded history" and BASELINES silently:
#   a first observation is not a transition, and a cold start must never page
#   for every clause that was already failing before anyone was watching.
#
# * **The episode is the key, never the build.** The index rebuilds every
#   ~180s. Keying a notification on the build would emit hundreds of pages for
#   one regression — the fleet has that mistake on record (an hourly timer
#   keyed on the failing RUN produced 5 CRITICALs and 5 RESOLVEDs for one
#   condition). An episode here is a maximal run of consecutive builds in which
#   one clause holds one non-MET status. It opens once, notifies once, and
#   clears once, symmetrically (`observability-policy.md` OB-7.2). An unchanged
#   rebuild writes nothing and notifies nothing.
#
# * **Recording is not delivery** (§7.2a). The transition is written to the
#   journal BEFORE any notification is attempted, and a delivery that fails
#   leaves the event on `pending_notifications` to be retried by the next
#   build. A page can be late; the record of the regression cannot be
#   conditional on the pager working.

#: The journal document's own version. Additive changes do not bump it; a
#: reader that must react does.
JOURNAL_SCHEMA_VERSION = 1

#: How many recorded transitions the pane renders. The journal keeps a year;
#: the pane shows the tail, because "what changed lately" is the question a
#: reader has and the full history is one JSON fetch away at the declared URI.
_RENDERED_TRANSITIONS = 10

#: What a transition is called on the wire and in the drain plane.
EVENT_KIND = "milestone-clause-transition"

#: Episode lifecycle, closed.
OPENED = "opened"
CLEARED = "cleared"

#: Why an episode closed. `recovered` is the clause returning to MET — the only
#: close that may be announced as a recovery. `superseded` is the clause moving
#: from one non-MET status to another (the canonical case: UNMET -> UNREPORTED,
#: which is a LOSS of measurement, not a fix); announcing that as a recovery
#: would be a lie of exactly the shape this module exists to prevent.
RECOVERED = "recovered"
SUPERSEDED = "superseded"


class JournalUnreadable(RuntimeError):
    """The journal object exists and could not be read.

    Distinct from "it is not there". A missing object is a cold start and
    baselines; an unreadable one must REFUSE — overwriting it would destroy the
    recorded history AND re-baseline every clause, which silently suppresses
    the very episode this exists to catch. Fail loud, write nothing, notify
    nothing, and say so on the surface.
    """


def journal(
    index: Any,
    *,
    numbers: Mapping[str, Any] | None = None,
    reader: Any = None,
    writer: Any = None,
    notifier: Any = None,
    now: Any = None,
) -> list[dict[str, Any]]:
    """Persist clause state, emit transitions, notify once per episode.

    Called ONCE PER BUILD (`config.build_index`), never per request: `evaluate`
    is a pure per-query function and a page refresh is not an observation.

    Every side-effecting collaborator is injectable and defaults to lazily
    constructed AWS clients, so the whole of this runs in tests with no
    credentials and no network — the hermeticity rule every adapter follows.

    A milestone with no `journal:` block is skipped entirely: nothing is read,
    nothing is written, nothing is notified. That is the state of
    `config.example.yaml` and of every unit test that does not ask for one.
    """
    declarations = [m for m in declared(index) if m.journal is not None]
    if not declarations:
        return []
    stamp = _now_iso(now)
    if numbers is None:
        numbers = _numbers_for(index)
    reports: list[dict[str, Any]] = []
    for milestone in declarations:
        reports.append(_journal_one(
            index, numbers, milestone, stamp,
            reader=reader, writer=writer, notifier=notifier,
        ))
    return reports


def _journal_one(index: Any, numbers: Mapping[str, Any], milestone: Milestone,
                 stamp: str, *, reader: Any, writer: Any,
                 notifier: Any) -> dict[str, Any]:
    spec = milestone.journal
    assert spec is not None  # guarded by the caller
    evaluated = _milestone(index, numbers, milestone)
    report: dict[str, Any] = {
        "milestone_id": milestone.id,
        "journal": _uri(spec),
        "exit_state": evaluated["exit_state"],
        "transitions": [],
        "notified": [],
        "written": False,
    }
    read = reader or _default_reader(spec)
    try:
        prior = read(spec.bucket, spec.object_key)
    except JournalUnreadable as exc:
        report["error"] = str(exc)
        return report
    baseline = prior is None
    prior = prior or {}

    doc, transitions, events = _next_journal(
        milestone, spec, evaluated, prior, stamp, baseline=baseline)
    report["transitions"] = transitions
    pending = list(prior.get("pending_notifications") or []) + events
    doc["pending_notifications"] = pending
    # The projection the pane renders: the transitions the DURABLE journal
    # holds, read back from it, newest last. Assembled here rather than by a
    # per-request S3 read — the console renders history, it does not fetch it
    # on every page view (§5.6, and a 180s build has no room for a request-path
    # round trip).
    report["recent"] = list(doc["transitions"])[-_RENDERED_TRANSITIONS:]
    report["open_episodes"] = [
        dict(e) for e in (doc.get("episodes") or {}).values()
    ]
    report["retention_hours"] = doc["history_retention_hours"]
    report["updated_at"] = stamp
    report["undelivered_notifications"] = 0

    heartbeat_due = _heartbeat_due(prior.get("updated_at"), stamp,
                                   spec.heartbeat_minutes)
    changed = bool(transitions) or bool(events) or baseline \
        or _snapshot(doc) != _snapshot(prior)
    if not (changed or heartbeat_due or pending):
        return report

    write = writer or _default_writer(spec)
    # RECORD BEFORE DELIVER (§7.2a). The regression is durable whether or not
    # the pager works; a delivery failure only leaves the event pending.
    write(spec.bucket, spec.object_key, doc)
    report["written"] = True

    if not pending:
        return report
    notify = notifier or _default_notifier(spec)
    still: list[dict[str, Any]] = []
    for event in pending:
        try:
            notify(event)
        except Exception as exc:  # transport failure: retry next build
            event["attempts"] = int(event.get("attempts") or 0) + 1
            event["last_error"] = f"{type(exc).__name__}: {exc}"
            event["last_attempt_at"] = stamp
            still.append(event)
            continue
        event["notified_at"] = stamp
        report["notified"].append(event)
        _mark_notified(doc, event, stamp)
    doc["pending_notifications"] = still
    doc["undelivered_notifications"] = len(still)
    report["undelivered_notifications"] = len(still)
    write(spec.bucket, spec.object_key, doc)
    return report


# ------------------------------------------------------- journal assembly --

def _next_journal(milestone: Milestone, spec: JournalSpec,
                  evaluated: Mapping[str, Any], prior: Mapping[str, Any],
                  stamp: str, *, baseline: bool
                  ) -> tuple[dict[str, Any], list[dict[str, Any]],
                             list[dict[str, Any]]]:
    """The journal this build should hold, the transitions it recorded, and the
    notification events those transitions owe."""
    prior_clauses = prior.get("clauses") or {}
    prior_episodes = dict(prior.get("episodes") or {})
    clauses: dict[str, Any] = {}
    transitions: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for clause in evaluated["clauses"]:
        cid = clause["id"]
        before = (prior_clauses.get(cid) or {})
        was = before.get("status")
        now_status = clause["status"]
        row = {
            "status": now_status,
            "label": clause["label"],
            "since": before.get("since") if was == now_status else stamp,
            "terms": _terms_snapshot(clause),
        }
        if clause.get("reason"):
            row["reason"] = clause["reason"]
        clauses[cid] = row
        if was == now_status:
            continue
        transition = {
            "at": stamp,
            "clause": cid,
            "label": clause["label"],
            "from": was,
            "to": now_status,
            "from_terms": before.get("terms") or [],
            "to_terms": row["terms"],
            "moved": _moved(before.get("terms") or [], row["terms"]),
        }
        if clause.get("reason"):
            transition["reason"] = clause["reason"]
        if was is None:
            # A first observation is not a transition anyone can be paged
            # about — there is no before. It is recorded so the NEXT move has
            # a baseline, and it is marked so nobody mistakes the record for a
            # regression.
            transition["baseline"] = True
            transitions.append(transition)
            if now_status != MET:
                prior_episodes[cid] = _episode(cid, clause, stamp, was,
                                               now_status, notified=False,
                                               baseline=True)
            else:
                prior_episodes.pop(cid, None)
            continue
        transitions.append(transition)
        open_episode = prior_episodes.get(cid)
        if open_episode and open_episode.get("status") != now_status:
            close_reason = RECOVERED if now_status == MET else SUPERSEDED
            closed = dict(open_episode, closed_at=stamp,
                          close_reason=close_reason, to=now_status)
            prior_episodes.pop(cid, None)
            # A clear is owed only for an episode that actually paged. Clearing
            # a page nobody received is noise, and it is how a recovery feed
            # stops meaning anything.
            if open_episode.get("notified"):
                events.append(_event(milestone, spec, evaluated, closed,
                                     CLEARED, stamp, transition))
        if now_status != MET:
            episode = _episode(cid, clause, stamp, was, now_status,
                               notified=True, baseline=False)
            prior_episodes[cid] = episode
            events.append(_event(milestone, spec, evaluated, episode, OPENED,
                                 stamp, transition))

    history = list(prior.get("transitions") or []) + transitions
    history = _bounded(history, spec, stamp)
    doc = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "milestone_id": milestone.id,
        "question": milestone.question,
        "tracker": milestone.tracker,
        "updated_at": stamp,
        # The whole predicate, as the ONE field a machine reads (deliverable
        # 4). `exit_confirmed` is the integer form the fleet gate grammar can
        # actually parse — `field <name> >= <num>` is the only content
        # predicate `gate_data_sweep._build_ready_when_re` accepts.
        "exit_state": evaluated["exit_state"],
        "exit_confirmed": evaluated["exit_confirmed"],
        "verified_when": evaluated.get("verified_when"),
        "clauses_met": evaluated["met"],
        "clauses_of": evaluated["of"],
        "clauses_unreported": evaluated["unreported"],
        "holding": list(evaluated["holding"]),
        "clauses": clauses,
        "episodes": prior_episodes,
        "transitions": history,
        # §8's retention contract, declared BY THE SOURCE so `console/history.py`
        # bounds a query against it rather than relabelling a short window.
        "history_retention_hours": int(spec.retention_days * 24),
        "first_observed_at": prior.get("first_observed_at") or stamp,
    }
    return doc, transitions, events


def _terms_snapshot(clause: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What each bound fact read, small enough to keep for a year of builds."""
    return [
        {"binding": f'{t["binding"]}:{t["ref"]}.{t["selector"]}',
         "value": t.get("value"), "op": t.get("op"), "target": t.get("target"),
         "status": t.get("status")}
        for t in clause.get("terms") or []
    ]


def _moved(before: Sequence[Mapping[str, Any]],
           after: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Which BINDING moved — the half of "what changed" a status alone cannot
    say. A clause is often several terms and only one of them moved; naming it
    is the difference between a page that locates the regression and a page
    that announces it."""
    prior = {t.get("binding"): t for t in before}
    out = []
    for term in after:
        was = prior.get(term.get("binding"))
        if was is None or was.get("value") != term.get("value"):
            out.append({
                "binding": term.get("binding"),
                "from": None if was is None else was.get("value"),
                "to": term.get("value"),
                "target": term.get("target"),
                "op": term.get("op"),
            })
    return out


def _episode(cid: str, clause: Mapping[str, Any], stamp: str, was: Any,
             status: str, *, notified: bool, baseline: bool) -> dict[str, Any]:
    return {
        "clause": cid,
        "label": clause["label"],
        "status": status,
        "from": was,
        "opened_at": stamp,
        "notified": notified,
        "baseline": baseline,
    }


def _bounded(history: list[dict[str, Any]], spec: JournalSpec,
             stamp: str) -> list[dict[str, Any]]:
    """Retention is a declared property of this artifact, applied here rather
    than left to grow forever — an object that only ever appends is one that
    eventually stops being writable inside a 180s build."""
    cutoff = _shift_days(stamp, -spec.retention_days)
    kept = [t for t in history if str(t.get("at") or "") >= cutoff]
    return kept[-spec.max_transitions:]


def _snapshot(doc: Mapping[str, Any]) -> Any:
    """What "unchanged" means. Deliberately EXCLUDES `updated_at` and the
    pending queue: a rebuild that read the same values must produce no write,
    or the object's own age stops being a liveness signal and 480 writes a day
    say nothing."""
    clauses = doc.get("clauses") or {}
    return sorted(
        (cid, row.get("status"), _wire(row.get("terms")))
        for cid, row in clauses.items()
    )


def _wire(value: Any) -> str:
    import json as _json
    return _json.dumps(value, sort_keys=True, default=str)


def _mark_notified(doc: Mapping[str, Any], event: Mapping[str, Any],
                   stamp: str) -> None:
    episode = (doc.get("episodes") or {}).get(event.get("clause_id"))
    if episode is not None and event.get("state") == OPENED:
        episode["notified"] = True
        episode["notified_at"] = stamp


# ------------------------------------------------------ the transition event --

#: Severity, per transition. A clause going UNMET is a declared exit criterion
#: regressing. A clause going UNREPORTED is the console LOSING THE ABILITY to
#: grade a declared exit criterion, which is not a lesser event: detection
#: blindness outranks the defects it hides, and a predicate that cannot be read
#: is the state in which a regression passes unseen. Both are `error`. The
#: clear is `info` — a recovery is news, not a page.
_SEVERITY = {UNMET: "error", UNREPORTED: "error"}


def _event(milestone: Milestone, spec: JournalSpec,
           evaluated: Mapping[str, Any], episode: Mapping[str, Any],
           state: str, stamp: str,
           transition: Mapping[str, Any]) -> dict[str, Any]:
    """One transition, as `observability-policy.md` §3's record shape.

    `identity_key` is the whole point: it is the SAME string for the page and
    for its clear, and it contains nothing about the build — no timestamp, no
    run id, no index generation. That is what makes the pair symmetric and what
    stops a 180s rebuild cadence turning one regression into hundreds of
    notifications.
    """
    clause_id = str(episode.get("clause"))
    cleared = state == CLEARED
    severity = "info" if cleared else _SEVERITY.get(
        str(episode.get("status")), "error")
    event: dict[str, Any] = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "kind": EVENT_KIND,
        "state": state,
        "severity": severity,
        # One condition, one identity, for the whole life of the episode.
        "identity_key": f"milestone:{milestone.id}:clause:{clause_id}",
        "dedup_key": f"milestone:{milestone.id}:clause:{clause_id}:"
                     f"{episode.get('opened_at')}",
        "milestone_id": milestone.id,
        "clause_id": clause_id,
        "clause_label": episode.get("label"),
        "tracker": milestone.tracker,
        "journal": _uri(spec),
        "occurred_at": stamp,
        "opened_at": episode.get("opened_at"),
        "from": transition.get("from"),
        "to": transition.get("to"),
        # The binding that MOVED, with its before and after values. A page
        # saying "clause 3 is now UNMET" sends a reader to five other surfaces;
        # a page saying "`number:population_completeness.unregistered` 0 -> 3,
        # target == 0" is the finding itself.
        "moved": list(transition.get("moved") or []),
        "terms": list(transition.get("to_terms") or []),
        # The milestone-level roll-up as of this build, so a reader knows
        # whether this is the clause that stopped the exit or one of several.
        "milestone_exit_state": evaluated["exit_state"],
        "clauses_met": evaluated["met"],
        "clauses_of": evaluated["of"],
        "clauses_unreported": evaluated["unreported"],
        "holding": list(evaluated["holding"]),
        "attempts": 0,
    }
    if cleared:
        event["close_reason"] = episode.get("close_reason")
    if transition.get("reason"):
        event["reason"] = transition["reason"]
    event["message"] = _message(milestone, event)
    event["subject"] = _subject(milestone, event)
    return event


def _message(milestone: Milestone, event: Mapping[str, Any]) -> str:
    lines = [_subject(milestone, event), ""]
    lines.append(f"milestone : {milestone.id}")
    lines.append(f"clause    : {event['clause_id']} — {event['clause_label']}")
    lines.append(f"status    : {event.get('from')} -> {event.get('to')}")
    if event.get("close_reason") == SUPERSEDED:
        lines.append(
            "note      : this clear SUPERSEDES the previous page; the clause "
            "did NOT recover, it moved to another non-MET status")
    for moved in event.get("moved") or []:
        lines.append(
            f"moved     : {moved.get('binding')} "
            f"{_render(moved.get('from'))} -> {_render(moved.get('to'))} "
            f"(target {moved.get('op')} {_render(moved.get('target'))})")
    if event.get("reason"):
        lines.append(f"reason    : {event['reason']}")
    lines.append(
        f"milestone : {event['milestone_exit_state']} — "
        f"{event['clauses_met']} of {event['clauses_of']} clauses met, "
        f"{event['clauses_unreported']} unreported")
    if event.get("holding"):
        lines.append(f"holding   : {', '.join(event['holding'])}")
    if milestone.tracker:
        lines.append(f"tracker   : {milestone.tracker}")
    lines.append(f"journal   : {event['journal']}")
    lines.append(f"identity  : {event['identity_key']}")
    return "\n".join(lines)


def _subject(milestone: Milestone, event: Mapping[str, Any]) -> str:
    verb = "RESOLVED" if event["state"] == CLEARED else "REGRESSED"
    if event.get("close_reason") == SUPERSEDED:
        verb = "SUPERSEDED"
    subject = (f"[{verb}] {milestone.id} clause {event['clause_id']}: "
               f"{event.get('from')} -> {event.get('to')}")
    # SNS caps a Subject at 100 characters and rejects the publish outright
    # above it — a page lost to a long clause id is a page lost.
    return subject[:99]


# -------------------------------------------------------------- transports --

def _default_notifier(spec: JournalSpec) -> Any:
    """The fleet alerting path, as TWO legs, both declared in config.

    §7.3: a human-only alert is invisible — the response plane cannot see it
    and nothing owns follow-through. So the operator leg (SNS) and the
    machine-readable leg (the EventBridge alert bus the Overseer drain
    consumes) are both attempted, and the notification counts as delivered only
    if EVERY declared leg succeeded. A half-delivered notification stays
    pending and is retried next build rather than being recorded as sent.

    With no `notify:` block the transition is still journaled and still
    rendered — it is simply not announced, and the journal says so
    (`undelivered_notifications`). Silent is a declared configuration here,
    never an accident.
    """
    notify = spec.notify
    if not notify.declared:
        def _record_only(event: Mapping[str, Any]) -> None:
            raise RuntimeError(
                "no `notify:` leg is declared for this milestone's journal — "
                "the transition is recorded but nothing announces it"
            )
        return _record_only

    def _publish(event: Mapping[str, Any]) -> None:
        from ..render.json import dumps as _dumps
        errors: list[str] = []
        if notify.sns_topic_arn:
            try:
                _sns(notify).publish(
                    TopicArn=notify.sns_topic_arn,
                    Subject=event["subject"],
                    Message=event["message"],
                )
            except Exception as exc:
                errors.append(f"sns: {type(exc).__name__}: {exc}")
        if notify.event_bus:
            try:
                _events(notify).put_events(Entries=[{
                    "EventBusName": notify.event_bus,
                    "Source": notify.event_source,
                    "DetailType": f"nousergon.{EVENT_KIND}.v"
                                  f"{JOURNAL_SCHEMA_VERSION}",
                    "Detail": _dumps(event),
                }])
            except Exception as exc:
                errors.append(f"eventbridge: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    return _publish


def _sns(notify: NotifySpec) -> Any:
    from ..aws import client as _client
    return _client("sns", notify.region)


def _events(notify: NotifySpec) -> Any:
    from ..aws import client as _client
    return _client("events", notify.region)


def _default_reader(spec: JournalSpec) -> Any:
    """Read the journal, distinguishing ABSENT from UNREADABLE.

    Returning `None` for a missing object and RAISING for an unreadable one is
    the whole contract: the first is a cold start that baselines, the second
    must refuse rather than overwrite a history it could not parse.
    """
    def _read(bucket: str, obj: str) -> dict[str, Any] | None:
        import json as _json
        from ..aws import client as _client
        s3 = _client("s3")
        try:
            body = s3.get_object(Bucket=bucket, Key=obj)["Body"].read()
        except Exception as exc:
            if _is_missing(exc):
                return None
            raise JournalUnreadable(
                f"s3://{bucket}/{obj} could not be read ({type(exc).__name__}: "
                f"{exc}) — refusing to overwrite it, because overwriting an "
                "unreadable journal would destroy the recorded history and "
                "re-baseline every clause"
            ) from exc
        try:
            doc = _json.loads(body)
        except ValueError as exc:
            raise JournalUnreadable(
                f"s3://{bucket}/{obj} is not valid JSON ({exc}) — refusing to "
                "overwrite it"
            ) from exc
        if not isinstance(doc, dict):
            raise JournalUnreadable(
                f"s3://{bucket}/{obj} is a {type(doc).__name__}, not a journal "
                "document — refusing to overwrite it"
            )
        return doc

    return _read


def _is_missing(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") \
        if hasattr(exc, "response") else None
    return code in {"NoSuchKey", "404", "NotFound"} \
        or type(exc).__name__ == "NoSuchKey"


def _default_writer(spec: JournalSpec) -> Any:
    def _write(bucket: str, obj: str, doc: Mapping[str, Any]) -> None:
        from ..aws import client as _client
        from ..render.json import dumps as _dumps
        _client("s3").put_object(
            Bucket=bucket, Key=obj,
            Body=_dumps(doc).encode("utf-8"),
            ContentType="application/json",
        )
    return _write


# ----------------------------------------------------------------- helpers --

def _uri(spec: JournalSpec) -> str:
    return f"s3://{spec.bucket}/{spec.object_key}"


def _numbers_for(index: Any) -> Mapping[str, Any]:
    """The §9 dict, assembled exactly as the landing view assembles it.

    Imported lazily because `render.json` imports THIS module — and assembled
    from the same helpers rather than a second copy, so the value a transition
    is recorded against is the value the page renders (§3.8, one computation
    behind both representations).
    """
    from ..render.html import is_exception
    from ..render.json import numbers as _numbers
    entities = index.all()
    return _numbers(index, [e for e in entities if is_exception(e)],
                    index.conflicts(), index.transparency_gap())


def _now_iso(now: Any = None) -> str:
    from datetime import datetime, timezone
    if isinstance(now, str):
        return now
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _shift_days(stamp: str, days: float) -> str:
    from datetime import datetime, timedelta, timezone
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - `_now_iso` always emits ISO
        return ""
    return (moment.astimezone(timezone.utc)
            + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _heartbeat_due(previous: Any, stamp: str, minutes: float) -> bool:
    """A journal rewritten only on change is indistinguishable, by age, from a
    journal nothing is writing any more. The heartbeat re-stamps `updated_at`
    on a declared cadence so the object's OWN staleness is readable — absence
    of a signal is never rendered as health (§5.5)."""
    from datetime import datetime
    if not previous:
        return True
    try:
        was = datetime.fromisoformat(str(previous).replace("Z", "+00:00"))
        now = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - was).total_seconds() >= minutes * 60
