# Adapters and drivers

> **Start here: you probably want a descriptor, not an adapter.** Onboarding a
> process or module is writing one file that binds it to where its facts
> already live (`console-policy.md` §2.6, `console/schemas/component_descriptor.schema.json`).
> No adapter, no console config entry, no edit to this repository. A **driver**
> reads a source *shape* and your descriptor says which one and where; an
> **adapter** enumerates a whole source the console's own config names. Reach
> for an adapter only when you need to discover things nobody has declared.


One adapter per source of truth. An adapter is a function from configuration to
entities and edges; it is the only thing that knows its source's shape. Cross-
source relations are formed by the index over entity identifiers, never inside
an adapter.

Every adapter **declares its claim class** (`console-policy.md` §2.5) — what
kind of statement its source is in a position to make. Several sources
describing one entity is the normal case, not a collision: the index merges
their claims by identifier, and the claim class is the precedence it merges
under.

| Class | The source is | Supplies | Adapters |
|---|---|---|---|
| `DECLARATION` | a registry | existence, `lifecycle`, owner, authority tier, declared cadence | `yaml-directory`, `declared-registry` |
| `OBSERVATION` | telemetry | state, as-of, run history, counts | `checks-envelope`, `state-machine`, `pipeline-reliability`, `git-host`, `object-store`, `sql-source`, `changelog-events`, `changelog-retro-feed`, `s3-records`, `sql-query` |
| `DISCOVERY` | a substrate enumeration | existence, and little else | `local-units`, `cloudwatch-metrics` |

## Drivers (`console/drivers/__init__.py::DRIVERS`)

A driver names a source **shape**, read from one component's own descriptor
binding (§2.7) — the opposite direction from an adapter, whose config names a
whole source the console enumerates. Registering a driver adds a shape the
whole fleet can bind to; it never adds knowledge of any component.

| Driver | Kinds | Cost | Reads |
|---|---|---|---|
| `object-store` | `artifact` | `CHEAP` | one declared key's last-modified stamp |
| `emitted-envelope` | `component`, `run`, `artifact` | `CHEAP` | one `console/emit.py`-shaped envelope |
| `document-fields` | `component` | `CHEAP` | one or more legacy JSON documents, named fields |
| `log-source` | `component` | `METERED` | a declared metric field out of a log window |
| `sql-source` | `component` | `METERED` | one parameterless `SELECT`'s single row |
| `s3-records` | any of the seven | `CHEAP` | one document, fanned out into N entities |
| `state-machine` | `run` | `CHEAP` | one declared state machine's own execution history |

## Before you write a new adapter: the boundary test

**Ruled 2026-08-11 (Brian, `nousergon-console#79`).** Four adapters implementing
one shape — *read an S3 object, find records in the body, project each onto a
typed entity* — were built within an hour by four concurrent sessions, each
having correctly checked the sibling issues and merged PRs first. None could see
another's in-flight branch. Consolidated onto `s3-records`; `object-store-records`
and `dated-snapshot` are retired.

So, in order, and stop at the first **yes**:

1. **Can a descriptor do it?** Then no adapter. See the note at the top of this
   file — that is the §2.6 path and it costs zero console changes.
2. **Does an existing adapter read this SOURCE SHAPE**, differing only in which
   bucket, prefix, table, query or key it points at? Then it is a config entry.
   *A different instance is never a different shape* (§2.7).
3. **Does an existing adapter read this shape but lack one capability?** Then
   fold that capability in, with a test, and reuse it. The three folded into
   `s3-records` by this ruling were `state_map`, `facets`, and a field `path`
   defaulting to the field's own name — small, and each made the surviving
   adapter strictly more capable.
4. **Only if none of the above**: a new adapter, whose docstring states which
   existing adapter it is NOT and why.

The two record-shaped adapters that legitimately stayed separate are the worked
example of step 4. `changelog-events` reads one fixed, shared, two-instance
schema rather than a generic shape. `object-store` projects a key's *existence*
onto an Artifact and never reads the body at all, which is why it still serves
non-JSON sources `s3-records` cannot parse. Neither is "S3 body → typed
entities" with different configuration.

**A different KIND is not a different shape either.** `s3-records` takes `kind`
from configuration precisely so that emitting a `Signal` here and a `Run` there
does not fork the adapter.

Two rules fall out of this and neither is negotiable:

- **A lifecycle disposition may only come from a declaration.** `DISABLED`,
  `DEPRECATED` and `RETIRED` are declared, never inferred
  (`observability-policy.md` §8.3) — telemetry structurally cannot tell a
  decision from a defect. A non-declaration claim proposing one is superseded
  and stays visible on the entity page rather than being discarded.
- **A declaration does not supply state.** It says what exists, not how it is
  doing, so its `UNREPORTED` loses to any live observation — otherwise every
  observed component would render as a transparency gap and the surface would
  report itself blind.

Every adapter:

- declares `CLAIM_CLASS` at module level
- takes **only** its own `config` subtree
- returns an `AdapterResult` (entities, edges, status, unavailable)
- treats source failure as `status=FAILED` with entities `UNREPORTED` — never
  an exception that empties the surface
- declares what it cannot supply in `unavailable`
- **declares `declared_cadence_seconds` — how often it is itself re-read**
- is hermetic in tests: the network/host call is one injectable function

`declared_cadence_seconds` is not just §5.9's whole-surface as-of. The index
stamps it onto every one of the adapter's entities as
`Provenance.cadence_seconds`, and §9.6 adds it to the threshold it compares
each row's `as_of` against — because an `as_of` is when the SOURCE last looked,
so a component polled every 900s cannot be observed fresher than 900s old
however healthy it is. **An adapter that omits it makes its rows unauditable**:
§9.6 excludes them from the denominator and names them in `unauditable` rather
than assuming an instantaneous observer (`alpha-engine-config-I7126`, where two
healthy 15-minute EventBridge probes flapped in and out of the violation set on
the phase offset alone). A TTL-cached adapter declares its TTL; an
uncached one declares the interval it is called on.

Adding a **source** is adding an adapter module and one line in
`console/config.py::ADAPTERS`. No other wiring changes.

**Adding a process or module is not that**, and reaching for an adapter is
usually the wrong move. A module is not a source — it writes into one. The
default path is the emission contract (`console/emit.py`,
`console-policy.md` §2.6): emit the published envelope where an enabled adapter
already reads, add the registry row, done — **no adapter, no config key, no
edit to this repository**. Onboarding cost is a published number with a target
of zero (§9.8), and every adapter written for something we ourselves write to
spends against it.

**Write an adapter when the source is one you do not control** — a vendor API,
a cloud control plane, someone else's registry — and say so in the PR (§12).

---

## `yaml-directory`

| | |
|---|---|
| **Reads** | A directory of YAML files, one component per file |
| **Emits** | `component` |
| **Cannot supply** | run history, freshness (unless the file carries it) |
| **Config** | `path`, `id_field` |

The registry adapter. Without a registry the console still runs: it indexes
whatever the other adapters return and reports population completeness as
unknown rather than as 1.0.

A row's optional **`cadence_minutes`** is carried onto the entity's `detail`.
It is the one field in §2.5's declaration column that changes another
source's verdict: a counter-reading adapter can say when something last ran
and never when it was *supposed* to, so a silent component is `UNREPORTED`
until a declaration says how often it should fire. With a cadence present the
merge resolves it to `MISSED` or `HEALTHY` — see
[`console/index/cadence_state.py`](../console/index/cadence_state.py). The
same field is what gives §9.6 `staleness_honesty` a row it can audit; a
non-positive or unparseable value is dropped rather than passed through, so a
declared-but-unusable cadence never reads as declared.

## `object-store`

| | |
|---|---|
| **Reads** | An S3-compatible bucket/prefix |
| **Emits** | `artifact` (and `produces`/`consumed-by` edges when the key pattern names a `component_id`/`consumer_id`) |
| **Cannot supply** | envelope body fields (status, summary, findings) — it never reads a body at all |
| **Config** | `bucket`, `prefix`, `key_pattern`, `cadence`, `staleness_factor`, `question` |

Projects **keys → Artifact entities**. Staleness is derived from last-modified
versus the configured cadence. Use this when the object *is* the fact (a
report, a snapshot) and freshness alone answers the declared question — a
markdown briefing, a parquet dump, or anything else `s3-records` cannot parse
(it reads JSON/CSV only). A declared `question` (`nousergon-console#61`)
renders as a synthetic `text` declared field, matching `s3-records`' own
convention for the same config key — the two adapters deliberately share
this one small piece of surface, since a source with no body to read still
answers `console-policy.md` §4.4. When the object is a **check-result
envelope** whose body carries status, prefer `checks-envelope`; when the
question needs the object's own numbers, prefer `s3-records`.

A declared `cadence` is also exposed as a plain `detail["cadence_minutes"]`
(alpha-engine-config-I7050), independent of the `state` this adapter already
derives from the same cadence above — that duplication is deliberate: it is
what lets `numbers.staleness_honesty()` independently RE-DERIVE the verdict
from `as_of` rather than trusting the state this adapter already assigned,
the entire point of an honesty check (§9.6). Previously unreachable for every
object-store-sourced artifact, checks-envelope was the only source class
`staleness_honesty` could ever audit.

## `checks-envelope`

| | |
|---|---|
| **Reads** | An S3-compatible prefix of `…/<check_id>/latest.json` envelopes |
| **Emits** | `component`, `run`, `artifact` |
| **Cannot supply** | anything outside the envelope (cost, baseline) |
| **Config** | `bucket`, `prefix`, `key_pattern`, `staleness_factor` |

Why not `object-store` plus config: the generic adapter projects keys to
Artifacts and derives staleness from last-modified alone. The fleet
check-result envelope carries `status` · `ran_at` · `cadence_minutes` inside
the body, and the entity kind that must render is **Component** (the check
*is* the component's reporting row). Parsing the body is a different
projection, so it is a different adapter.

`check_id` is used **verbatim** as the component id — never slug-minted. A
dying check's last write is almost always `ok`; `ran_at` + `cadence_minutes`
are what mark it `MISSED` when publishing stops — the schedule fired or should
have and no run started, which is a failure upstream of the component, in its
trigger. Not `STALLED` (nothing reported a start) and not `FAILED` (it did not
stop, it never began).

Envelope shape (schema_version 1):

```json
{
  "schema_version": 1,
  "check_id": "example-check",
  "label": "Example check",
  "ran_at": "2026-07-31T12:00:00+00:00",
  "status": "ok",
  "summary": "one line an operator can act on",
  "cadence_minutes": 60,
  "deep_link": "https://…",
  "findings": []
}
```

## `state-machine`

| | |
|---|---|
| **Reads** | A configured list of state-machine ARNs (Step Functions shape) |
| **Emits** | `run`, `cycle`, `artifact` |
| **Cannot supply** | `cost` (executions carry no cost tag by default) |
| **Config** | `region`, `state_machines`, `cycle_key` |

One Run per execution (id = execution ARN), one Cycle per distinct value of
the configured `cycle_key` field in the execution input, and Artifact edges
where input/output name durable keys (`s3://…` or explicit `artifact_key` /
`output_key` fields).

**Horizon honesty.** The reader must page **fully**. The adapter stamps
`horizon_earliest` · `horizon_latest` · `horizon_execution_count` ·
`horizon_machines_read` · `horizon_complete` onto every Cycle so a truncated
history cannot pass as complete. A silently truncated execution list is the
forbidden shape at the data layer.

Production deployments inject a boto3-backed reader; the library ships none,
so an enabled adapter without a reader returns `FAILED` / `unavailable=reader`
rather than silently zero rows.

## `state-machine` (driver)

| | |
|---|---|
| **Reads** | One state machine a component's own descriptor names (`state_machine_arn`) |
| **Emits** | `run` |
| **Cannot supply** | `cost` (executions carry no cost tag by default); Cycle or Artifact entities |
| **Config** | `state_machine_arn`, `region`, `cadence_minutes` (or `cadence_seconds`/`cadence_hours`) |

The **driver** twin of the `state-machine` **adapter** above — same precedent
as `object-store` and `s3-records` existing as both
(`nousergon-console#99`). The adapter reads a console-configured LIST of
state machines and builds Run + Cycle + Artifact entities with full
horizon-honesty paging; this driver reads ONE state machine a component's own
descriptor names, and emits Run entities only — no Cycle/Artifact/horizon-
honesty machinery, since that is the adapter's job for a fleet-wide
configured list and a component descriptor has nowhere to declare a
`cycle_key` or durable-key field names the way the adapter's console config
does.

**The status mapping itself is not reimplemented.** Both this driver and the
`state-machine` adapter import it from
`console/state_machine_shape.py::run_state` — the SF execution-status ->
twelve-state mapping (`SUCCEEDED`→`HEALTHY`, `FAILED`/`ABORTED`→`FAILED`,
`TIMED_OUT`→`STALLED`, `RUNNING`/`PENDING`/`PENDING_REDRIVE`→`HEALTHY` with
the source status carried in `detail`, everything else→`UNREPORTED`) — so the
two callers can never drift apart on what one status means (§2.3), the same
discipline `console/records_shape.py` set for the `s3-records` adapter/driver
pair (`nousergon-console#98`).

One Run per execution (id = execution ARN). Production deployments inject a
boto3-backed reader via `context["execution_reader"]`; the library ships
none, so a binding without a reader returns a failed `DriverResult` /
`unavailable=reader` rather than silently zero runs — the same convention
`object-store`'s driver uses for its `object_stat` injection point.

## `pipeline-reliability`

| | |
|---|---|
| **Reads** | The same state-machine ARNs as `state-machine`, plus an injected trading calendar |
| **Emits** | `cycle` (one per cycle day, six-value reliability classification), `signal` (first-attempt success rate, attempts-to-success, rerun revisit trigger, market-open buffer trend) |
| **Cannot supply** | `DEGRADED` for a pipeline that declares neither `degraded_state_names` nor a degraded terminal error; stage depth for a pipeline that declares no `stage_states`; buffer trend for a pipeline with no `open_time`/`open_timezone` |
| **Config** | `region`, `state_machines` (`arn`, `pipeline_key`, `measure_buffer`, `degraded_state_names`, `degraded_error_names`, `cadence`, `cadence_weekdays`, `noop_max_duration_seconds`, `stage_states`, `cutovers`, `rerun_alert_threshold`), `role_field`, `cadence_roles`, `recovery_roles`, `window_trading_days`, `open_time`, `open_timezone` |

Same source shape as `state-machine`, a different projection: one Cycle per
trading day (id `pipeline-reliability:<pipeline_key>:<date>`), classified
`SUCCEEDED` / `FAILED-recovered` / `FAILED-unrecovered` / `DEGRADED` /
`HOLIDAY` / `NEVER-FIRED` — a domain vocabulary for a trading day, not a
member of §8.3's twelve component states (Cycle is outside
`COMPONENT_STATE_KINDS`, so it carries this value verbatim per §5.1's second
half). `HOLIDAY` comes from the injected `TradingDayChecker`, never from
execution history: a Step Function's own terminal status is identical whether
it ran end-to-end or was skipped for a market holiday, so the split needs the
calendar. `NEVER-FIRED` is a trading day with zero cadence-role executions —
the schedule should have fired and did not.

`DEGRADED` arrives on either of two axes, and both are the same fact. A
pipeline that degrades under the **Option-A** shape ends in a `Fail` state
carrying `Error: DegradedRun` on purpose, so status-keyed watchers engage —
`degraded_error_names` (default `[DegradedRun]`) matches it from the execution
summary alone, no extra API call. A pipeline that instead signals degradation
by *succeeding* having entered a marker state declares `degraded_state_names`,
which needs a richer read of the SAME source (`GetExecutionHistory`, one extra
call per execution, fetched only when declared). `DEGRADED` is honestly absent
when a pipeline declares neither.

> Deriving `DEGRADED` as *first attempt succeeded AND entered a degraded state*
> — the pre-`alpha-engine-config-I6919` rule — is a conjunction no Option-A
> pipeline can satisfy, because Option-A makes the degraded run FAIL. The
> bucket was unreachable for exactly the pipelines that degrade honestly, and
> every one of their degraded runs rendered as an ordinary red.

**Cycle days are the pipeline's own cadence, not the market's.** `cadence`
picks how the window's *expected* days are enumerated: `trading-day` (default,
consults the calendar), `weekday` with `cadence_weekdays` (Mon 0 .. Sun 6), or
`calendar-day`. A Saturday-scheduled weekly pipeline run through the NYSE
calendar windows none of its own cycle days and renders every one of them
`HOLIDAY` — the absence state collapsing into "not expected", which is what
`NEVER-FIRED` exists to prevent.

**Attempts-to-success is the headline, not success rate.** A cycle that
succeeds on rerun 6 and one that succeeds first time both read as "succeeded"
without it, and the difference between them is the whole question. `attempts`
counts the scheduled run plus every operator overlay against the same cycle;
roles outside `cadence_roles ∪ recovery_roles` land in `execution_count` only.
`noop_max_duration_seconds` excludes a cadence execution that self-gated and
terminated immediately — counting a run that did no work as a success inflates
the numerator the same way counting operator reruns does.

**Stage depth renders how far a run got.** `stage_states` is an ordered list of
state names from that pipeline's own definition; each cycle carries
`stage_reached` / `stage_depth` / `stage_count`, so a run failing later than the
last one reads as progress rather than as another red. `cutovers`
(`{date, label, ref}`) marks dated changes to the pipeline on the strip, because
a regression whose cause is a known cutover is a different fact from an
unexplained one.

The `rerun-revisit-trigger` Signal implements `sf-pipeline-policy.md` §1's
*">1 operator rerun to reach a complete, non-degraded terminal"* revisit
trigger, which was otherwise a declared trigger with no detector. A cycle that
never reached success breaches it too: zero reruns that worked is not evidence
the threshold was respected.

First-attempt success rate, attempts-to-success, the rerun trigger and the
buffer trend all render via the self-describing `fields` mechanism
(`model/fields.py` §5.8) — no bespoke rendering code.

Ships no default trading-calendar implementation in the base install: the
`calendar` extra (`pandas-market-calendars`) is a generic, unaffiliated OSS
NYSE calendar, never a fleet-specific one, so a standalone deployment of this
console never inherits an alpha-engine dependency by installing it. Without
the extra or an injected `trading_day_checker`, the adapter returns `FAILED`
/ `unavailable=("trading_calendar",)` — but **only when some configured
pipeline actually declares a `trading-day` cadence**. A weekly pipeline needs no
market calendar, and refusing to render it for want of one its window never
consults is the same false-unavailable in the other direction.

## `git-host`

| | |
|---|---|
| **Reads** | A Git host's issue/PR trackers (via `gh` by default) |
| **Emits** | `decision`, `incident`, `artifact` (PRs, when `include_prs: true`) |
| **Cannot supply** | anything outside the tracker |
| **Config** | `org`, `repos`, `incident_label`, `include_prs`, `include_workflow_runs` |

**`include_workflow_runs`** emits one **Component** per workflow the repo
declares, at the id the fleet's own discoverer derives —
`<repo>-<workflow-file-stem>`, slugified identically — so a registry
declaration and this observation MERGE (§2.5) rather than rendering the same
workflow twice. The lister enumerates workflows FIRST and joins runs onto them,
so a scheduled workflow that has **never fired** renders `NEVER_RAN` instead of
being invisible, which is the most serious finding available here. A run in
flight keeps the last concluded state with `detail.in_flight` set — §8.3's
twelve have no in-progress member (`alpha-engine-config-I6358`), and both
alternatives are worse: a thirteenth state breaks the closed vocabulary, and
`UNREPORTED` would blink a healthy nightly job to "nothing reported" every night
while it ran.

Identifiers are the tracker refs the host assigns, never console-minted.
Issues: `<repo>-I<N>`. With `include_prs: true`, an **open** PR joins the
queue at `<repo>-PR<N>` — its own namespace, so an issue and a PR sharing a
number never collide — and a **merged** PR becomes an Artifact at
`<org>/<repo>#<N>`. A closed, unmerged PR answers neither "waiting on Brian"
nor "what merged" and is not emitted. `include_prs` defaults false.

## `changelog-events`

| | |
|---|---|
| **Reads** | An S3-compatible prefix of one JSON object per event (schema 1.0.0) |
| **Emits** | `incident` (plus `produces` edges from the body's own `source`/feeder field) |
| **Cannot supply** | anything outside the event body |
| **Config** | `bucket`, `prefix`, `key_pattern`, `id_template`, `state_field` or `state_literal` |

Why not `object-store` plus config: that adapter always projects keys to
Artifacts and derives staleness from last-modified. This source's entity kind
is `incident`, and its state is a declared field read from the body
(`state_field`, e.g. `severity`) or a literal that applies to the whole
prefix (`state_literal`) — never key staleness. Two configured instances of
this one adapter cover both changelog prefixes: the raw event-lake
(`changelog/entries/`, `state_field: severity`) and its vocab-quarantine
sibling (`changelog/quarantine/`, `state_literal: quarantined`) — one schema,
one adapter, per `policy-shared-code`'s second-adoption rule.

`id_template` builds the identifier from the `key_pattern`'s named groups —
`{event_id}` when the source assigns a unique id across the whole prefix,
`{day}/{event_id}` when uniqueness only holds within a day partition (the
quarantine case). Lineage is derived from schema 1.0.0's own `source` field
(the feeder that wrote the entry), never inferred: no per-deployment config
needed because the schema itself already names the producer.

## `changelog-retro-feed`

| | |
|---|---|
| **Reads** | ONE S3-compatible JSON document containing a pre-grouped array |
| **Emits** | `incident` (one per group, `{subsystem}\|{summary}` id) |
| **Cannot supply** | narrative for a group nobody has written up yet |
| **Config** | `bucket`, `key`, `cadence` |

The opposite shape from `changelog-events`: one key holds many entities,
already grouped by an upstream aggregator. State is `ready-for-retro` /
`needs-triage` from the document's own `has_writeup` flag — not a member of
observability-policy.md §8.3's twelve, because a retro group is not a
component. A `ready_for_retro` entry with a matching group is merged in as
`detail.resolution`; the adapter performs no filtering of its own — the
upstream emitter already excludes non-incident events before writing the
document.

## `declared-registry`

| | |
|---|---|
| **Reads** | One YAML document (a list of entries, or an id-keyed mapping) |
| **Emits** | Any one kind, configured — never Component/Run state, only a declared `lifecycle` |
| **Cannot supply** | state for a Component/Run target (a declaration never supplies state) |
| **Config** | `path`, `kind`, `id_field`, `state_field`, `default_state`, `entries_field` |

Generalises `yaml-directory` ("a directory of files, one Component per file")
to "one file, many entries, any kind" — for a registry that does not have that
shape, e.g. a declared inventory of load-bearing Artifacts or a declared
inventory of Decision-kind rollout gates.

**This is what makes "missing" computable for an Artifact.** `object-store`
can only say what a bucket prefix actually contains; it has no notion of what
*should* be there. A `declared-registry` DECLARATION claim, merged (§2.5) by
identifier against an `object-store` OBSERVATION claim pointed at the same
keys, is what turns "declared, never showed up in a listing" into a rendered
fact. Configure `default_state` to the same token `object-store`'s own driver
uses for a confirmed-missing artifact (`"absent"`) so the single surviving
claim — when nothing observes it — reads correctly on the exception list.

A target kind of Decision needs no merge at all: an observation registry's own
gate value (gated-off / gated-on / always-on) *is* the fact nothing else
observes, so the lone claim renders unmerged.

**Calendar-aware cadence (`console/calendar_cadence.py`, alpha-engine-config-
I7050).** A registry entry may declare `cadence: saturday_sf|weekday_sf|eod_sf|
continuous|event_driven` instead of a literal `cadence_minutes` — the shape
the fleet's own `ARTIFACT_REGISTRY.yaml` uses, one refresh symbol per row
rather than a flat minute count that cannot tell a real gap from an ordinary
weekend or holiday. When present (and no literal `cadence_minutes` already
is), the adapter translates it into an effective, `now`-relative minute
ceiling — trading-day symbols (`weekday_sf`/`eod_sf`) via an injectable
`trading_day_checker` (same contract as `pipeline-reliability`'s, defaulting
to `pandas-market-calendars` under the `calendar` extra), `saturday_sf` via
plain calendar-week arithmetic, `continuous` from a declared
`interval_minutes`. `event_driven` — and any symbol this module cannot
resolve, including a trading-day symbol with no reachable calendar —
deliberately gets **no** `cadence_minutes` at all: excluded from
`staleness_honesty`'s denominator (§5.3), never faked into reading fresh.
This is what makes a `declared-registry` + `object-store` merge (above)
independently auditable by §9.6, not just able to answer "missing".

## `sql-source` (adapter)

| | |
|---|---|
| **Reads** | One SQL query (sqlite by default; inject `connect` for another engine) |
| **Emits** | Any one kind, configured, one entity per returned row |
| **Cannot supply** | anything outside the query's own columns |
| **Config** | `database`, `query`, `kind`, `id_fields`, `id_separator`, `state_field`, `default_state`, `component_id_field`, `field_descriptors` |

The `drivers/sql_source.py` **driver** is one component's own descriptor
naming one parameterless `SELECT` whose single row becomes that component's
metrics. This adapter is the same source shape, opposite direction (same
precedent as `object-store` existing as both an adapter and a driver): the
console's own config names a query that returns **many** rows, each becoming
one entity keyed by a composite identifier (`id_fields`, joined by
`id_separator`) — for a question that does not belong to any one component's
descriptor, e.g. a per-(phase, ticker) data-integrity Signal.

Every non-identifier column becomes a §5.8 declared field automatically —
`field_descriptors` lets a deployment supply `unit`/`baseline`/`render` for a
column that needs one; without it the field still renders, undecorated,
never dropped.

`component_id_field` is optional: when a row names the component it is
about (e.g. a `process_id` column on an SLA hit-rate row), the adapter derives
a `measures` edge from the row's Signal to that Component. The index derives
the reverse (`measured-by`), so the Signal appears as a related entity — a
facet — on the Component's own entity page with no new rendering path.

## `s3-records`

| | |
|---|---|
| **Reads** | An S3-compatible prefix whose objects carry zero, one, or many per-instance records (JSON or CSV) |
| **Emits** | Whichever entity kind the config declares — `component`, `run`, `cycle`, `artifact`, `signal`, `decision`, `incident` |
| **Cannot supply** | anything not reachable by a declared field `path` |
| **Config** | `bucket`, `prefix`, `key_pattern`, `kind`, `question`, `id_template`, one of `records_path` (optionally with `group_field`) / `array_fields` / `format: csv`, `state_field`/`state_default`/`state_map`, `as_of_field`, `evidence_template`, `fields`, `facets` |

**State resolution for `component`/`run`**, in order: `state_map` translates the
source's own vocabulary (`{"passed": "HEALTHY"}`) into
`observability-policy.md` §8.3's twelve, then a direct match on a state name.
Three outcomes stay three facts (§5.5) — **no value** renders `UNREPORTED`
(nothing reported), a value **nothing can interpret** renders `DEGRADED`
(something reported, uninterpretable — a finding), and a `state_map` entry
naming a state that does not exist also renders `DEGRADED`, because a typo in
the map must never read as healthy.

**`facets`** map record or body paths onto the fields §2.2 filters on uniformly
across the index — a different thing from declared `fields`, which are
*rendered*. A facet whose path resolves to nothing is omitted, never written as
an empty string: absent and `""` filter differently.

**A field's `path` defaults to its own name**, so `{score: {render: value}}`
reads `score`. An explicit `path` still wins.

Generalizes `object-store` past "keys → Artifact" to **any** kind, by making
the entity kind and every field a config declaration instead of Python. Reach
for this over a bespoke adapter when a source you do not control already
carries everything a row needs — the same JSON/CSV a legacy consumer reads —
so nothing about *what a field means* is compiled in (§2.3's "one adapter per
source" is satisfied by config, not by a new module per source instance).

Three ways one key's body becomes N entities, picked by which config key is
present (`records_path` and `array_fields` are mutually exclusive; `format:
csv` ignores both — the whole file is the record list):

- **Whole-body** (none declared): the object is the one record — one Cycle
  per `consolidated/{date}/eod_report.json`.
- **`records_path`** (a dotted path to a JSON array of objects): fan out one
  entity per list item — one Decision per ticker in an artifact's `tickers`
  array.
- **`records_path` with a `*` segment, plus `group_field`**
  (`nousergon-console#57`): `*` iterates a DICT at that point in the path
  rather than indexing a named key, injecting its key under `group_field`
  onto every record reached beneath it — the same mechanism reaches a nested
  dict-then-array (`"tiles.*.components"`: a report card's per-tile
  MetricRecords, tile name injected) and a dict OF records
  (`"loops.*"`: an apply-audit's per-loop outcomes, loop id injected), since
  neither shape is a JSON array at any single dotted path the plain
  `records_path` case above can name.
- **`array_fields`** (a list of equal-length array field names): zip
  index-wise into one record per index — a source with no per-instance object
  at all, only parallel arrays (`tickers`, `target_weights`, …).
- **`format: csv`**: each row is a record.

`id_template` is a Python format string resolved against regex named groups
∪ body-level scalars ∪ the current record (record wins on collision) — e.g.
`"{date}:{ticker}"`. Every declared `fields` entry's `path` resolves against
the **nested** merge of body ∪ record, so a field can reach either a
per-record value or a body-level one (`optimizer_cfg.risk_aversion`) through
the same dotted syntax. `question` (`console-policy.md` §4.4) is carried
through as a synthetic `text` declared field, so the pane renders the
question with no kind-specific rendering code.

`state_field`/`state_default` resolve the same way: for `component`/`run`
(§8.3's twelve-state kinds) the resolved value is mapped through `State` by
name, defaulting to `UNREPORTED` when nothing matches; for every other kind
the raw value renders verbatim (§5.1's "otherwise the value itself"). With
neither declared, state falls back to the `object-store` freshness convention
(`fresh`/`stale`/`no-freshness-stamp`/`no-cadence-declared`/`unreadable`).

## `s3-records` (driver)

| | |
|---|---|
| **Reads** | One document a component's own descriptor names (`key`) |
| **Emits** | Whichever entity kind the binding declares — `component`, `run`, `cycle`, `artifact`, `signal`, `decision`, `incident` |
| **Cannot supply** | anything not reachable by a declared field `path` |
| **Config** | `key`, `kind`, `format` (default `json`), one of `records_path` (optionally with `group_field`) / `array_fields` / `format: csv`, `state_field`/`state_default`/`state_map`, `as_of_field`, `evidence_template`, `fields`, `facets`, `cadence_minutes` |

The **driver** twin of the `s3-records` **adapter** above — same precedent as
`object-store` existing as both (`nousergon-console#98`, closing the gap
`alpha-engine-config-I7477`'s report-card v3 console binding hit). The adapter
enumerates a whole prefix the console's config names; this driver reads ONE
document a component names in its own descriptor, and applies the identical
fan-out grammar to project N entities from it — e.g. one Signal per
`report_card.json`'s `tiles.*.components` row, bound from
`crucible-evaluator`'s own `console.descriptor.yaml` rather than
`config.example.yaml`.

**The grammar itself is not reimplemented.** Both this driver and the
`s3-records` adapter import it from `console/records_shape.py` — one module
implementing whole-body / `records_path` list / grouped `*` fan-out with
`group_field` / `array_fields` parallel arrays / CSV, plus id-template
resolution, `fields` extraction (§5.8) and `state_field`/`state_map`
resolution (§5.1/§5.5) — so the two callers can never drift apart on what one
shape means (§2.3).

Two differences from the adapter, both a consequence of reading one document
instead of listing a prefix: there is no key-pattern regex, so `id_template`
and `facets` resolve only against body-level and per-record values (no
capture-group context); and freshness comes from an injected `object_stat`
(the same injection point `object-store`'s driver uses) rather than the
lister's per-key last-modified stamp.

Use `s3-records` (driver) over `document-fields` when one document must fan
out into **several** entities (a report card's per-tile rows, an audit's
per-loop outcomes); use `document-fields` when several documents combine into
**one** component's own fields.

## `sql-query`

| | |
|---|---|
| **Reads** | Named `SELECT` queries against a SQLite-shaped database |
| **Emits** | `signal`, `decision`, `run` (raw-value kinds; `component`/`run` need an explicit `state_map` or `default_state`) |
| **Cannot supply** | anything outside the configured query's own columns |
| **Config** | `db_path`, `queries` (`name`, `entity_kind`, `query`, `id_template`, `state_field`, `state_map`, `default_state`, `as_of_field`, `evidence_template`, `facets`, `detail_columns`, `json_columns`) |

Distinct from the `sql-source` **driver**: that driver reads one row bound to
one already-known component, from a spec in a component descriptor (a file
committed beside the component — a public repo means the spec itself must
never carry a credential, hence its `credential` indirection). This adapter
is the many-row counterpart, used when a query's rows ARE the entities — a
Signal per ticker-date, a Decision per ticker-eval_date, a Run per team
cycle. Its config lives in the console's own gitignored `config.yaml`, so a
literal `db_path` is the same shape as `object-store`'s literal `bucket` — no
credential indirection needed at this layer.

Every query is validated as one parameterless `SELECT` before anything runs.
Two claims from different queries about the SAME identifier merge by
identifier (§2.5) — useful when two tables each know something about one
row; a query proposing a *different* raw `state` for that identifier
produces a §2.5 conflict, so prefer a SQL join over two competing claims when
one row's state must stay singular.

`entity_kind: run`/`component` resolve to §8.3's closed vocabulary, never a
raw string — via `state_map` (raw column value → state name) or
`default_state` (a row's mere presence declares this state, for a query that
enumerates completed cycles with no failure column of their own). Neither is
hardcoded here: which opinion a schema licenses belongs in the query
binding's config. A row resolving neither renders `UNREPORTED` and is named
in `unavailable`.

`json_columns` decodes a column holding a JSON string (e.g. SQLite's own
`json_group_array(json_object(...))` aggregate) into a structured `detail`
value — the mechanism a `GROUP BY` query uses to carry a drill-to-rows list
(§3.4) inside one entity's row, with no second query and no bespoke
rendering.

## `local-units`

| | |
|---|---|
| **Reads** | The host's systemd inventory |
| **Emits** | `component`, `run` |
| **Cannot supply** | intended state (that lives in the registry) |
| **Config** | `unit_prefixes` |

A unit present here but absent from the registry renders `UNREGISTERED` at
index time; a registry row with no unit renders `ABSENT`. Both are findings,
and they are different findings. `inactive` is `UNREPORTED` — whether resting
is a finding needs the registry, which an adapter must not read, so this
adapter cannot place it and says so loudly rather than guessing. `masked` is
the one inactive case carrying operator intent the source itself supplies, and
it renders `DISABLED`.

## `cloudwatch-metrics`

| | |
|---|---|
| **Reads** | One CloudWatch metric namespace, enumerated by a dimension |
| **Emits** | `component` |
| **Cannot supply** | `cadence` (lives in the registry), `cost` |
| **Config** | `region`, `namespace`, `dimension`, `invocations_metric`, `errors_metric`, `window_minutes`, `history_days`, `cadence_seconds`, `discovery_facet`, `discovery_value`, `id_pattern` |

The dimension value **is** the component id, verbatim, so a registry
declaration and this observation merge instead of double-rendering (§3.6).
Generic: `AWS/Lambda`/`FunctionName` is one config entry, and a second
instance pointed at another namespace/dimension pair needs no code.

A `DISCOVERY` claim, not an `OBSERVATION`. Its primary statement is *the
substrate has this thing* — it learns what exists by enumerating the
namespace, not by anything reporting in — and that is also the only thing
that makes `ABSENT` computable here: a registry row the namespace has never
heard of is a finding, not a blank. Its state readings therefore rank below a
real emitted envelope and above a bare declaration, which is correct for a
reading taken from the substrate's counters rather than from the component.

**Scope.** `discovery_scope` on the result (`discovery_facet`/
`discovery_value` in config) names the facet slice this pass enumerated. Without
it the index would read one namespace's successful pass as licence to assert
`ABSENT` over every substrate in the registry — GitHub Actions workflows and
launchd agents included — turning "I did not look there" into "it is not
there". A pass declaring no scope claims the whole fleet, which is what every
discovery adapter predating the field meant.

**Zero is never green.** `errors > 0` is `FAILED`; `invocations > 0` with no
errors is `HEALTHY`; zero invocations with no datapoint in the history lookback
is `NEVER_RAN`. Zero invocations *with* history behind it is `UNREPORTED` with
the reason on the row — idle-by-design and missed-its-trigger are `DISABLED` vs
`MISSED`, and metrics alone cannot separate them. Only a declared cadence can,
and §2.3 forbids reading the registry that would carry one. So the row stays a
finding and stays counted in the transparency gap until a cadence is declared.
Rendering it `HEALTHY` would lower that number by treating no-data as good
news, which is the failure the number exists to detect.

**Cost.** `GetMetricData` bills per metric requested. `cadence_seconds` is a
cost control as much as a freshness declaration: the adapter serves its own TTL
cache inside that interval and declares the cadence on the result, so §5.9
bounds freshness by what was actually read rather than by when the index last
rebuilt. Reading on the 60-second rebuild costs 15x a 900-second cadence for
data summarised over a 1440-minute window either way. The history lookback is
issued only for components that were silent in the window — anything that
invoked has already answered the question that pass exists to ask.
