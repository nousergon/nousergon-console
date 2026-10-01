# Console contracts

This file is the **normative public contract** for the parts of the console other people build against: adapters, drivers, component descriptors, how claims from several sources merge, the emission envelope, and the JSON representation every view serves. These rules used to live in the private console policy (retired) and were moved here so they sit beside the code and tests that enforce them. A change to this file may only **strengthen** a contract or **record a reasoned delta** (what changes, why, what replaces the lost guarantee, and when it is revisited). Loosening a rule to match what was built is not an admissible change.

> **The `## ` headings below are anchors.** A migration checker resolves each one by its exact text, so do not rename, renumber or reword them. Each section names the obligation ids it carries (`Obligations: …`) so the maintainer's private ledger can trace them.

**Authority.** Amending any contract here is reserved to the maintainer (the repository owner), who rules by merging the PR. The rules below are carried as they were written; where a detail could not be published it was stated generically and is listed under [Redactions](#redactions).

For the per-adapter and per-driver reference, see [adapters.md](adapters.md). For the rules every change is held to, see [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Adapter contract

Obligations: `CN-2.3-one-adapter-per-source-no-cross-adapter-coupling`, `CN-2.3-one-adapter-per-source-no-cross-adapter-coupling.2`, `CN-2.3-one-adapter-per-source-no-cross-adapter-coupling.3`, `CN-2.3-one-adapter-per-source-no-cross-adapter-coupling.4`, `CN-2.3-one-adapter-per-source-no-cross-adapter-coupling.5`, `CN-2.3-one-adapter-per-source-no-cross-adapter-coupling.6`, `CN-2.3-one-adapter-per-source-no-cross-adapter-coupling.7`, `CN-2.6-bespoke-adapter-needs-written-reason`, `CN-2.6-bespoke-adapter-needs-written-reason.2`, `CN-11-fleet-only-adapter-may-be-public`.

**The adapter interface is the product's public contract and the console's internal module boundary at once.** An adapter is a function from configuration to entities and edges. It knows **exactly one source** — an object-store prefix, a YAML registry directory, a GitHub API surface, a Step Functions execution history, a metric backend, a systemd or launchd inventory — and nothing else knows that source at all.

- **Adapters do not know about each other.** An adapter that reads a second source, or reaches into another's output to enrich its own, is forbidden: it has become the coupling this boundary exists to prevent. Cross-source relations are formed by the index over entity identifiers, never inside an adapter.
- **Adapters are configured, never hardcoded.** Every literal naming a bucket, key pattern, ARN, instance, port, path or component comes from configuration. A topology literal in adapter source is a build finding.
- **An adapter declares what it cannot supply.** A source with no freshness stamp, no record counts or no baseline says so, and its entities carry the corresponding state from the fleet observability state vocabulary — never a silent default.
- **An adapter failing is an entity state, not an exception.** An unreachable source renders its entities `UNREPORTED` and the adapter itself `FAILED`; it never empties the surface and never removes rows.
- **Adding a source is adding an adapter** — no change to the entity model, the index, the router, or any pane. If a new source requires touching those, either the entity model is wrong or the source is two sources.

**Test:** delete an adapter. Exactly its own entities disappear — no pane breaking, no other adapter changing, and no route 404ing on a different kind.

**A bespoke adapter is still the exception and still needs a written reason**, reserved for a source the operator does not control: a vendor API, a cloud control plane, someone else's registry. A source we write to is a source a driver can read (see [Driver contract](#driver-contract) and [Component descriptor and onboarding](#component-descriptor-and-onboarding)). **Adding a bespoke adapter carries, in its PR, a written reason why no driver plus a descriptor binding was sufficient.** An adapter with no such reason is onboarding work that should have cost nothing.

**Exception (exhaustive; add by PR only).** An adapter for a source only the maintainer's own deployment has **may ship in this public repo**, provided every rule above holds and it carries no topology literal. A generic "read a YAML registry directory" adapter is publishable even if its only user today is the maintainer; a read-our-buckets adapter is not an adapter, it is configuration that failed to become configuration.

Where this is enforced:

- The return shape — `AdapterResult` with `status`, `unavailable`, and `FAILED`/`UNREPORTED` on an unreachable source — is [`console/model/envelope.py`](../console/model/envelope.py); the base is [`console/adapters/base.py`](../console/adapters/base.py).
- Every adapter declares a claim class: [`tests/test_claim_merge.py`](../tests/test_claim_merge.py) (`test_every_adapter_declares_a_claim_class`); one unreachable source does not blank an entity: `test_a_failed_adapter_downgrades_its_own_claim_not_the_merged_entity` in the same file.
- Serving a stale-and-marked index rather than an empty one when a source fails: [`tests/test_index_freshness.py`](../tests/test_index_freshness.py).
- The adapter checklist and the four-step boundary test to run before writing a new adapter: [adapters.md](adapters.md#before-you-write-a-new-adapter-the-boundary-test). No topology in source and the delete bar are also review rules in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Claim merge

Obligations: `CN-2.5-claims-merge-by-identifier-never-collide`, `CN-2.5-claims-merge-by-identifier-never-collide.2`, `CN-2.5-claims-merge-by-identifier-never-collide.3`, `CN-2.5-claims-merge-by-identifier-never-collide.4`, `CN-2.5-claims-merge-by-identifier-never-collide.5`, `CN-2.5-claims-merge-by-identifier-never-collide.6`, `CN-2.5-claims-merge-by-identifier-never-collide.7`.

**Two adapters describing the same thing is the normal case, not a collision.** The registry *declares* a component, telemetry *observes* it, and a substrate enumeration *discovers* it — three sources, one `component_id`. So an adapter returns **claims**, not entities-of-record, and the index merges every claim sharing an identifier into one entity, resolving field-level conflicts under a declared precedence:

| Rank | Claim class | Supplies |
|---|---|---|
| 1 | **Declaration** — the registry | existence, `lifecycle`, `owner`, `authority_tier`, declared cadence and retention |
| 2 | **Observation** — telemetry | state, as-of, run history, counts |
| 3 | **Discovery** — a substrate enumeration | existence, and nothing else |

- **A declared lifecycle is never overridden by an observation.** `DISABLED`, `DEPRECATED` and `RETIRED` are declared, never inferred.
- **Every merged field keeps the claim it came from**, so a row's `source` stays truthful on a merged row: a row assembled from three sources names three sources, per field.
- **A conflict is rendered, never resolved silently and never fatal.** Where two claims of equal rank disagree on a field, the entity carries both, renders `DEGRADED`, and names the disagreement. Where precedence settles it, the losing claim stays visible on the entity page.
- **The merge is over the same identifier for the same kind only.** The one-namespace rule is unchanged: two different things sharing an identifier is still a build error, and `kind` disagreement is the test that distinguishes them.

**Forbidden:** an adapter enriching its own output from another adapter's (merging is the index's job and only the index's) · a merge that discards a claim rather than ranking it · a duplicate identifier raising an exception that empties or truncates the surface · any lifecycle disposition inferred from an observation.

Where this is enforced: the merge is [`console/index/merge.py`](../console/index/merge.py); the claim classes and their ranks are `ClaimClass` in [`console/model/envelope.py`](../console/model/envelope.py). [`tests/test_claim_merge.py`](../tests/test_claim_merge.py) is the chokepoint — one test per clause, including `test_a_merged_row_names_a_source_per_field_not_the_last_adapter`, `test_a_declared_lifecycle_survives_an_observation_that_says_otherwise`, `test_an_observation_may_not_invent_a_declared_lifecycle`, `test_equal_rank_disagreement_renders_rather_than_picking`, `test_a_conflict_never_empties_or_truncates_the_surface` and `test_two_different_kinds_under_one_identifier_still_raise`. Merged provenance and conflicts reaching the wire: [`tests/test_json_representation.py`](../tests/test_json_representation.py). Build-time namespace uniqueness: [`tests/test_declared_namespace.py`](../tests/test_declared_namespace.py).

## Component descriptor and onboarding

Obligations: `CN-2.6-onboarding-costs-no-console-code`, `CN-2.6-onboarding-costs-no-console-code.2`, `CN-2.6-onboarding-costs-no-console-code.3`, `CN-2.6-onboarding-costs-no-console-code.4`, `CN-2.6-metric-read-by-field-never-regex`, `CN-2.6-log-location-referenced-not-restated`.

**A process or module is not a source — it writes into one.** So onboarding a process or module is **writing one file**: a descriptor that names the component and binds it to the places its facts already live — its runs, its artifacts, its metrics, its inputs and outputs. Nothing about the console changes, and in the normal case nothing about the module changes either.

The maintainer's ruling, 2026-08-03: modules log the metrics they need to expose, and the console points to the source of the logging or database that holds them.

**The descriptor is committed beside the thing it describes** and read by the registry adapter. It carries identity, lifecycle, owner and authority tier (the declaration claim — see [Claim merge](#claim-merge)), and the **source bindings**:

| Binding | Names | Answers |
|---|---|---|
| **runs** | a state machine, a unit, a schedule, a workflow | did it run, did it finish, did it miss |
| **artifacts** | keys or tables, with a declared cadence | what it produced, and whether that is fresh |
| **metrics** | a log location or a query, plus the [declared field descriptors](#declared-field-descriptors) | its own numbers |
| **lineage** | keys it consumes and produces | who breaks if this is stale |

- **A descriptor references the fleet's declared log location rather than restating it.** A second declaration of a log location is forbidden.
- **A metric is read from a structured record, addressed by field name — never by a pattern matched against prose.** A metric extracted by regex from unstructured log text is forbidden. A declared field that stops appearing renders as a finding naming the field (for example, *the field `rows_written` was absent from the last N records*).

**Test:** onboard a component that does not exist yet and count the edits to the console repository and to the console's configuration. **Both are zero, or this contract is not met** — and the count is a standing published number (the onboarding-cost number), not a claim made once at review. Adding the component's own descriptor is not an edit to the console.

Threshold: **0** console-repo edits and **0** console-config edits per onboarded component.

**Forbidden:** a per-module rendering path, template or plugin · an adapter written for one component · a source binding that lives in the console's configuration rather than in the component's descriptor · a metric extracted by regex from unstructured log text · a second declaration of a log location · onboarding documented as "ask whoever wrote the adapter".

Where this is enforced: the published descriptor schema is [`console/schemas/component_descriptor.schema.json`](../console/schemas/component_descriptor.schema.json), parsed by [`console/model/descriptor.py`](../console/model/descriptor.py). [`tests/test_descriptor_bindings.py`](../tests/test_descriptor_bindings.py) is the chokepoint: `test_a_component_in_a_place_no_adapter_points_at_onboards_for_free`, `test_the_binding_is_in_the_descriptor_not_in_console_config`, `test_log_source_reads_a_declared_json_metric_by_field_name` and `test_log_source_reports_unstructured_records_and_a_missing_field`. Zero console edits on the emission path: [`tests/test_emission_contract.py`](../tests/test_emission_contract.py) (`test_a_module_onboards_with_zero_console_edits`). The standing onboarding-cost number: [`tests/test_onboarding_cost.py`](../tests/test_onboarding_cost.py) and [`console/index/onboarding.py`](../console/index/onboarding.py).

## Declared field descriptors

Obligations: `CN-5.8-declared-fields-rendered-generically`, `CN-5.8-declared-fields-rendered-generically.2`, `CN-5.8-declared-fields-rendered-generically.3`, `CN-5.8-declared-fields-rendered-generically.4`, `CN-5.8-declared-fields-rendered-generically.5`, `CN-5.8-declared-fields-rendered-generically.6`, `CN-12-render-hint-addition-is-a-policy-pr`.

A generic surface renders a module's own numbers **without knowing anything about that module**, by requiring the emission to describe its own fields — never by growing a renderer per domain.

**Every field beyond the model's own carries a descriptor: `type`, `unit`, `baseline` (or an explicit declaration of none), and one `render` hint from the closed vocabulary** `value` · `duration` · `bytes` · `ratio` · `count` · `timeseries` · `link` · `text`. The console renders any declared field generically from its descriptor, on the entity page and as a facet where the type admits one.

- **`unit` is required**, carried in the descriptor or in the field name.
- **`baseline: none` is a declaration**, and the number then renders as telemetry rather than being coloured a verdict.
- **An undeclared field renders as opaque text and is counted, never dropped.**

**Test:** emit a field the console has never seen, from a module the console has never heard of. It renders, with its unit, without a console change.

**Forbidden:** rendering code keyed on a component id, a repo or a domain · a render hint outside the closed vocabulary · a numeric field with no unit · silently dropping an unrecognised field.

**Changing the vocabulary.** Adding a render hint to the closed vocabulary is a PR against this contract, ruled by the maintainer merging it. The set is closed because an open one becomes a plugin API, and a plugin API is a per-module rendering path with a nicer name.

Where this is enforced: [`console/model/fields.py`](../console/model/fields.py). [`tests/test_declared_fields.py`](../tests/test_declared_fields.py) is the chokepoint: `test_the_console_renders_a_field_it_has_never_seen`, `test_no_rendering_code_is_keyed_on_who_emitted_something`, `test_a_number_without_a_unit_renders_saying_so`, `test_baseline_null_is_a_DECLARATION_and_renders_as_telemetry`, `test_a_bare_value_with_no_descriptor_renders_opaque_and_is_counted` and `test_the_render_vocabulary_is_closed_and_matches_the_published_schema`. Facets: [`tests/test_declared_field_facets.py`](../tests/test_declared_field_facets.py). Undeclared fields from a driver: [`tests/test_document_fields_driver.py`](../tests/test_document_fields_driver.py).

## Driver contract

Obligations: `CN-2.7-drivers-read-a-shape-never-an-instance`, `CN-2.7-drivers-read-a-shape-never-an-instance.2`, `CN-2.7-drivers-read-a-shape-never-an-instance.3`, `CN-2.7-driver-declares-cost-class-no-per-request-paid-query`, `CN-12-adding-a-driver-declares-its-contract`.

A **driver** is the source reader named from the descriptor's side: a descriptor says `driver: object-store, key: …` and the console knows how to read it. **One driver per source shape** — object store, state machine, local unit, log location, query, git host, emitted envelope.

- **Drivers are generic and public; instances are configuration.** Which group, bucket, ARN or table belongs to a component is in that component's descriptor. **A driver that knows a component is forbidden.**
- **A driver declares what it cannot supply and its cost class**, and is read on the refresh pass, never per request. **A per-request query against a paid or rate-limited source is forbidden.**
- **A binding that fails is an entity state, not an exception** (as for adapters — see [Adapter contract](#adapter-contract)), and `doctor` names which binding failed.

**Forbidden:** a driver that knows a component · a descriptor naming a driver that does not exist, without the build saying so · a per-request query against a paid or rate-limited source.

**Adding a driver** carries, in its PR: its claim class (see [Claim merge](#claim-merge)), the retention it declares per signal, what it cannot supply and what it costs, and the source *shape* it reads — never an instance.

Where this is enforced: the registry of shapes is `DRIVERS` in [`console/drivers/__init__.py`](../console/drivers/__init__.py), listed with cost classes in [adapters.md](adapters.md#drivers-consoledrivers__init__pydrivers). [`tests/test_descriptor_bindings.py`](../tests/test_descriptor_bindings.py): `test_no_driver_source_names_an_instance` (the instance lint) and `test_the_instance_lint_actually_catches_one`, `test_an_unknown_driver_fails_the_build_loudly`, `test_every_driver_declares_a_cost`, `test_one_components_broken_binding_does_not_blank_the_others` and `test_a_failed_binding_is_named_by_doctor`. `doctor` itself: [`tests/test_doctor.py`](../tests/test_doctor.py).

## Emission envelope

Obligations: `CN-2.6-emission-envelope-is-a-published-versioned-contract`, `CN-12-envelope-change-is-versioned-with-overlap`.

**Emission ([`console/emit.py`](../console/emit.py)) is one binding kind, for a module with nowhere durable to put a number — not the default.** Most facts are already written down somewhere, and the default path is a descriptor pointing at them (see [Component descriptor and onboarding](#component-descriptor-and-onboarding)).

The envelope remains part of the public contract: **versioned, published with its schema, carried by a client library, every field optional with a declared default.** An envelope field whose meaning is agreed only in conversation, rather than in the published schema, is forbidden.

**Changing the envelope is a versioned schema change:** every field optional with a declared default, the version bumped, and the **previous version still ingested for a stated period**. An emitter is not obliged to redeploy because the console changed.

Where this is enforced: the published schema is [`console/schemas/component_report.schema.json`](../console/schemas/component_report.schema.json); the client library and its `SCHEMA_VERSION` are [`console/emit.py`](../console/emit.py). [`tests/test_emission_contract.py`](../tests/test_emission_contract.py) is the chokepoint: `test_a_module_onboards_with_zero_console_edits`, `test_every_schema_field_is_optional`, `test_an_empty_report_is_valid_and_renders_as_a_finding` and `test_declared_fields_survive_to_the_entity_uninterpreted`. Emission as one binding kind: `test_emission_is_now_one_binding_kind_not_the_path` in [`tests/test_descriptor_bindings.py`](../tests/test_descriptor_bindings.py).

## API — same-URL JSON representation

Obligations: `CN-3.8-every-view-serves-json-at-the-same-url`, `CN-3.8-every-view-serves-json-at-the-same-url.2`, `CN-3.8-every-view-serves-json-at-the-same-url.3`.

Scope: every instance of nousergon-console.

**Every route serves both an HTML and a JSON representation of the same query, at the same URL**, selected by the `Accept` header or a `.json` suffix. The JSON is the versioned index projection — entities, edges, per-field provenance, and the console's self-grading numbers — and it is **the same query the HTML renders**, never a parallel endpoint set.

**Test:** for any page, a program can obtain exactly what that page shows, from that page's URL, carrying a version it can assert on.

**Forbidden:** an HTML-only route · an API whose query surface differs from the UI's · a JSON payload with no `schema_version` · an API requiring an authentication story separate from the edge identity in front of the whole surface.

Where this is enforced: content negotiation is [`console/server/negotiate.py`](../console/server/negotiate.py), the router [`console/server/router.py`](../console/server/router.py), and the JSON renderer (with `SCHEMA_VERSION`) [`console/render/json.py`](../console/render/json.py). [`tests/test_json_representation.py`](../tests/test_json_representation.py) is the chokepoint: `test_every_route_serves_both_representations`, `test_both_representations_come_from_the_same_resolved_request`, `test_accept_header_is_the_primary_signal`, `test_a_404_answers_in_the_representation_that_was_asked_for` and `test_every_payload_carries_a_schema_version`. The self-grading numbers in both representations: [`tests/test_nine_numbers.py`](../tests/test_nine_numbers.py).

## Redactions

This repository is public, so some details in the source obligations were stated generically here. The rule is unchanged in each case; only the identifying detail was removed.

- `CN-2.6-log-location-referenced-not-restated` — the path of the fleet's private log-location registry file (in a private repository) was generalised to "the fleet's declared log location".
- `CN-3.8-every-view-serves-json-at-the-same-url`, `.2`, `.3` — the hostname of the maintainer's own instance was dropped from the scope line; the scope is "every instance of nousergon-console", which includes it.
- `CN-2.6-onboarding-costs-no-console-code.3` (and the authority line of every record) — the maintainer's personal name was replaced with "the maintainer (the repository owner)"; the ruling's date and substance are carried as written.
