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
| `DECLARATION` | a registry | existence, `lifecycle`, owner, authority tier, declared cadence | `yaml-directory` |
| `OBSERVATION` | telemetry | state, as-of, run history, counts | `checks-envelope`, `state-machine`, `git-host`, `object-store` |
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
| **Emits** | `decision`, `incident` |
| **Cannot supply** | anything outside the tracker |
| **Config** | `org`, `repos`, `incident_label` |

Identifiers are the tracker refs the host assigns — `<repo>-I<N>` /
`<repo>-PR<N>` — never console-minted.

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
