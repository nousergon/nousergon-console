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
| `OBSERVATION` | telemetry | state, as-of, run history, counts | `checks-envelope`, `state-machine`, `pipeline-reliability`, `git-host`, `object-store`, `sql-source`, `changelog-events`, `changelog-retro-feed`, `s3-records`, `sql-query`, `object-store-records` |
| `DISCOVERY` | a substrate enumeration | existence, and little else | `local-units` |

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
- is hermetic in tests: the network/host call is one injectable function

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

## `object-store`

| | |
|---|---|
| **Reads** | An S3-compatible bucket/prefix |
| **Emits** | `artifact` (and `produces` edges when the key pattern names a `component_id`) |
| **Cannot supply** | envelope body fields (status, summary, findings) |
| **Config** | `bucket`, `prefix`, `key_pattern`, `cadence`, `staleness_factor` |

Projects **keys → Artifact entities**. Staleness is derived from last-modified
versus the configured cadence. Use this when the object *is* the fact (a
report, a snapshot). When the object is a **check-result envelope** whose body
carries status, prefer `checks-envelope`.

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

## `pipeline-reliability`

| | |
|---|---|
| **Reads** | The same state-machine ARNs as `state-machine`, plus an injected trading calendar |
| **Emits** | `cycle` (one per trading day, six-value reliability classification), `signal` (first-attempt success rate, market-open buffer trend) |
| **Cannot supply** | `DEGRADED` for a pipeline that declares no `degraded_state_names`; buffer trend for a pipeline with no `open_time`/`open_timezone` |
| **Config** | `region`, `state_machines` (`arn`, `pipeline_key`, `measure_buffer`, `degraded_state_names`), `role_field`, `cadence_roles`, `recovery_roles`, `window_trading_days`, `open_time`, `open_timezone` |

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

`DEGRADED` needs a richer read of the SAME source (`GetExecutionHistory`,
opt-in per pipeline via `degraded_state_names` — costs one extra API call per
execution, never fetched by default) and is honestly absent when a pipeline
declares no degraded state names.

First-attempt success rate and the buffer trend render via the self-describing
`fields` mechanism (`model/fields.py` §5.8) — no bespoke rendering code.

Ships no default trading-calendar implementation in the base install: the
`calendar` extra (`pandas-market-calendars`) is a generic, unaffiliated OSS
NYSE calendar, never a fleet-specific one, so a standalone deployment of this
console never inherits an alpha-engine dependency by installing it. Without
the extra or an injected `trading_day_checker`, the adapter returns `FAILED`
/ `unavailable=("trading_calendar",)` rather than treating every day as a
trading day, which would make `NEVER-FIRED` and `HOLIDAY` indistinguishable.

## `git-host`

| | |
|---|---|
| **Reads** | A Git host's issue/PR trackers (via `gh` by default) |
| **Emits** | `decision`, `incident`, `artifact` (PRs, when `include_prs: true`) |
| **Cannot supply** | anything outside the tracker |
| **Config** | `org`, `repos`, `incident_label`, `include_prs` |

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
| **Config** | `bucket`, `prefix`, `key_pattern`, `kind`, `question`, `id_template`, one of `records_path` (optionally with `group_field`) / `array_fields` / `format: csv`, `state_field`/`state_default`, `as_of_field`, `evidence_template`, `fields` |

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

## `object-store-records`

| | |
|---|---|
| **Reads** | One or more explicitly-named JSON objects |
| **Emits** | `artifact`, `signal`, `decision`, `incident`, `cycle` (raw-value kinds only — `component`/`run` are rejected at fetch time) |
| **Cannot supply** | anything outside the body |
| **Config** | `bucket`, `keys`, `entity_kind`, `records_path`, `id_template`, `body_as_of_field`, `state_field`, `default_state`, `facets`, `evidence_template` |

Why not `object-store` plus config: that adapter projects a key's *existence*
onto one Artifact. This adapter reads a key's *content*, finds a configured
list of records inside the body (`records_path`, a dotted path), and
projects EACH RECORD into its own entity — the shape a snapshot artifact
takes when its value is a scored universe (one row per ticker) rather than a
single fact about the object. Same "same source shape, different
projection" split as `checks-envelope` vs. `object-store`.

`id_template` and `state_field` (dotted path) read from the record itself; a
top-level body field (`body_as_of_field`, default `as_of`) is injected into
every record's format context under `as_of` — but only when the record
carries no such key of its own, so a genuine per-record field is never
overwritten. Nested sub-objects (a record's own `gate`, `pillars`, `metrics`)
pass into `detail` untouched.

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
