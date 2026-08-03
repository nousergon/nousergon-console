# Adapters

One adapter per source of truth. An adapter is a function from configuration to
entities and edges; it is the only thing that knows its source's shape. Cross-
source relations are formed by the index over entity identifiers, never inside
an adapter.

Every adapter:

- takes **only** its own `config` subtree
- returns an `AdapterResult` (entities, edges, status, unavailable)
- treats source failure as `status=FAILED` with entities `UNREPORTED` — never
  an exception that empties the surface
- declares what it cannot supply in `unavailable`
- is hermetic in tests: the network/host call is one injectable function

Adding a source is adding an adapter module and one line in
`console/config.py::ADAPTERS`. No other wiring changes.

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
