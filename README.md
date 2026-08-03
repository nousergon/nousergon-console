# nousergon-console

**A read-only fleet index that persists nothing and reports what it cannot see.**

Most monitoring surfaces are a pile of dashboards. A dashboard is a frozen answer to the questions its author had at authoring time — but monitoring is the business of being asked questions nobody anticipated: *why is this stale · what else touched that artifact · what did this cycle cost · which of these has never run · who breaks if this one does.* Each of those is a traversal, not a tile.

This is an **index over a typed entity graph** instead. Point it at the artifacts you already have; it builds a catalog you can search, link to, and walk.

> **Status: initial implementation.** The contract below is settled and normative; the adapter layer, the entity index, search and the server are implemented. The implementation stack is Python + stdlib server — see [docs/stack-decision.md](docs/stack-decision.md) and the [Roadmap](#roadmap).

## What makes it different

|  | Service / metadata catalogs | This |
|---|---|---|
| **Target** | teams on cloud-native stacks (Kubernetes, Terraform, dbt, Airflow) | one operator or a small team, over heterogeneous sources — object-store keys, cron jobs, systemd units, state machines, YAML registries, CI |
| **State** | a database plus ingestion pipelines | **persists nothing** — every figure is a projection of a fact durable somewhere else |
| **Headline number** | catalog coverage | **the transparency-gap count** — how much of your system the surface cannot see |

That last row is the point. A surface that renders nine of fourteen services *in depth* is worse than a coarse one rendering all fourteen, because the five it omits are indistinguishable from five that are fine. So this one publishes its own blind spots as a first-class metric, and the objective for that number is zero.

If you already run Backstage, Port, OpenMetadata or DataHub over a standard cloud-native stack, use those — their connector libraries are the reason they exist. This is for the case where none of your sources has a stock connector.

## The entity model

Everything rendered is a fact about exactly one of seven kinds:

| Kind | Is | Identified by |
|---|---|---|
| **Component** | anything that runs unattended and can fail with no human present | `component_id` |
| **Run** | one execution of a component | run id |
| **Cycle** | the business period runs belong to — a day, a weekly cadence, a deploy | cycle id |
| **Artifact** | a durable thing produced or consumed — an object key, a table, a report | its key |
| **Signal** | one named measurement over time, with its baseline | metric name |
| **Decision** | a ruling, a gate, a queued question, a policy clause | tracker reference |
| **Incident** | a failure record with its severity and class | incident id |

**Cost is not a kind.** It is a facet of Run, Cycle and Component, so *"what did this cycle cost"* is a traversal rather than a separate system with its own component list that will disagree with yours.

## Three ways to reach everything

Every entity is reachable **by name** (search), **by structure** (navigation), and **by relation** (a link from anything adjacent). Three paths, independently sufficient — because a fact reachable only one way is reachable only by someone who already knows it exists, and that is a forensic tool rather than a measurement.

The relation direction that matters most is the reverse one. *What did this produce* is usually written down somewhere. *Who breaks if this is stale* exists nowhere unless the index derives it — and it is the only question an incident actually asks.

## Getting a process or module onto it

**A module emits, adds a registry row, and appears — with zero edits to this repository and zero edits to the console's configuration.** That is the default path, and it is the whole path:

```python
from console.emit import report, write_json

write_json(
    report(
        component_id="my-nightly-job",
        status="ok",                      # about this RUN: ok · attention · error
        cadence_minutes=1440,             # without this, staleness is not computable
        summary="processed 903 tickers, 0 rejected",
        deep_link="https://ci.example/run/1234",
        consumes=["s3://bucket/upstream.parquet"],
    ),
    "/var/lib/reports/my-nightly-job/latest.json",
)
```

**Every field is optional with a declared default.** A required field added later is a fleet-wide breaking change dressed as a schema improvement, and it lands on every emitter at once — most of which nobody is going to redeploy. The schema is published at `console/schemas/component_report.schema.json` and is part of the product contract.

A module's own numbers come along **without any rendering code that knows about it**. `fields` carries a descriptor per value:

```python
fields={
    "tickers_scanned": {"value": 903, "unit": "tickers",
                        "baseline": 900, "render": "count"},
    "p99_latency":     {"value": 4.2, "unit": "s",
                        "baseline": None, "render": "duration"},
}
```

- **`unit` is required for a number.** A measurement whose unit is inferred from context is the defect that emitted a normalized ratio, consumed it as raw share volume, and silently failed 901 of 903 tickers for months.
- **`baseline: null` is a declaration**, and the number then renders as telemetry — plain, uncoloured. Green means *better than the baseline*; where there is no baseline there is no colour. An absent baseline and a `null` one are different facts and render differently.
- **An unrecognised field renders opaque and is counted, never dropped.** A dropped field is a fact the emitter believes is on the surface and is not, and it fails silently on their side of a boundary they cannot see.
- The `render` set is **closed** — an open one becomes a plugin API, and a plugin API is a per-module rendering path with a nicer name.

The cost of onboarding the next thing is a **published number** (target: zero edits). A surface whose coverage is bounded by how much adapter code somebody felt like writing will always render a subset while looking complete.

**Writing an adapter is the exception**, not the path — reserved for sources you do not control: a vendor API, a cloud control plane, someone else's registry. A source you write to is a source you can make emit.

## Every view is also JSON, at the same URL

```
curl -H 'Accept: application/json' https://console.example/component/my-job
curl https://console.example/component/my-job.json          # same thing
curl https://console.example/.json                          # the landing view
```

**Agents are a first-class reader of this surface**, not an afterthought. One they cannot read forces every agent to re-derive system state from raw sources — and the agent's picture and the operator's picture then diverge exactly when something is wrong, because that is when the derivations differ.

It is not an API *beside* the UI: the resolver runs **once** and both representations render from its result. There is one router and one query, so the two cannot drift in coverage — a route that exists serves both, a route that does not 404s in both, and adding a route cannot add it to one and forget the other.

- `Accept: application/json` is primary. `*/*` deliberately does **not** count — a browser sends it, and defaulting that to JSON would make the human surface unreachable.
- The `.json` suffix is a fallback, and only applies to a path that does not already resolve — because an artifact's identifier **is** its object key, so `/artifact/ops/checks/x/latest.json` names a real entity.
- A 404 answers in the representation that was asked for. An agent that gets an HTML error page has to parse prose to learn what happened.
- Every payload carries `schema_version`.

## Adapters

One adapter per source of truth. An adapter is a function from configuration to entities and edges, and it is the **only** thing that knows its source's shape.

- Adapters do not know about each other. Cross-source relations are formed by the index over entity identifiers, never inside an adapter.
- **Every literal naming a bucket, ARN, host, port, path or component comes from configuration.** None is compiled in — which is what makes this artifact usable by anyone but its author.
- An adapter declares what it cannot supply. A source with no freshness stamp or no baseline says so, and its entities carry the corresponding state rather than a silent default.
- An unreachable source renders its entities `UNREPORTED` and the adapter `FAILED`. It never empties the surface and never removes rows.
- Adding a source is adding an adapter — no change to the model, the index, the router, or any view.

## Configuration

`config.example.yaml` is the tracked shape; your real `config.yaml` is gitignored. It declares which adapters are enabled, what each points at, and which facets matter in your fleet. Nothing about your topology belongs anywhere else in this repository.

## Rendering rules

Every rendered fact carries four fields — **state · source · as-of · evidence link**. A dot that cannot say how it knows is not yet trustworthy.

- A row older than its declared cadence renders **stale**, not as its last value in normal styling.
- Any roll-up states its denominator inline (`12 / 14 reporting`). One that cannot is not rendered.
- A number with no baseline is rendered as telemetry, never coloured as a verdict.
- Zero, null, empty, never-ran, never-triggered and never-observed are different facts and render as different things. **No data is never drawn as green, and never drawn as nothing.**
- Component state comes from one closed, total vocabulary of twelve, and it has **no fall-through**. There is no `UNKNOWN`, no `PENDING`, no `N/A`: where the classifier cannot place a component the answer is `UNREPORTED`, which is loud and is a finding. What a component is deliberately *not* doing — `DISABLED`, `DEPRECATED`, `RETIRED` — is **declared**, never inferred, because a decision and a defect are indistinguishable from telemetry alone.
- Not everything is a component. An artifact is fresh or stale; an issue is open or closed. Those rows carry the source's own value rather than being forced into a vocabulary that has no word for them.
- Every addressable state has a URL built from entity identifiers. Paste it anywhere; it reproduces the view on a cold load.
- **The index has an as-of too, and it bounds every row's.** Every page and every payload carries the index build time, the rebuild cadence, and each source's read. A surface built once at start and served all day renders every row frozen at boot *while looking exactly like a live one* — and that is invisible precisely because the rows stay internally consistent with each other. Past the shortest cadence its sources declared, the **whole surface** says so; a rebuild that fails keeps serving the previous index and marks it, rather than emptying the surface or pretending to be current.

## Roadmap

1. The adapter contract and reference adapters — **done** (filesystem/YAML registry, object store, checks-envelope, state-machine, Git host API, systemd units). See [docs/adapters.md](docs/adapters.md).
2. The entity index, its URL scheme, and the relation graph — **done**.
3. Generated navigation, global search, entity pages — **done**.
4. The seven self-grading numbers, published on the surface itself.

The implementation stack is Python + stdlib server — decided in [docs/stack-decision.md](docs/stack-decision.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). One thing worth stating up front: this is a dogfooded tool, so its roadmap is driven by its authors' own use until there is a second real user. A feature request grounded in your actual use is welcome and is exactly what moves that line.

## Licence

AGPL-3.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
