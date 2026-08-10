# Dashboard → Console migration mapping table

**Source:** `alpha-engine-config#6131` deliverables 1–2 (`console-policy.md` §13 rebuild step 4). This
document is the mapping table (deliverable 1) and the named fork merges (deliverable 2). It does **not**
perform the migration (deliverable 3) or the retirement PR (deliverable 4) — those are staged as follow-up
issues, listed at the bottom.

**Measured against:** `crucible-dashboard/views/*.py` at HEAD, 2026-08-10. **75 files** (72 at the
2026-07-31 measurement cited in the issue — the tree grew by 3 in the interim), **51 numbered-prefix**
files (49 previously). Prefix collisions, unchanged in kind: `27_` (2-way: Active_Observations,
Flow_Doctor_Heartbeat), `36_` (2-way: LLM_Usage, Predictor_Training), `50_` (3-way: Data_Integrity,
Expenses, System_State), `54_` (2-way: Fleet_SLA, PR_Pipeline). **Prefix collision (§3.6) is a distinct
defect from a content fork (§4.4)** — three of the four collision groups below turned out to be
topically unrelated files that merely grabbed the same number; only the `54_` group has any relationship
to the fork analysis, and it's a partial one. The new console's identifiers are typed (`component_id`,
run id, etc.), never a sequential prefix, so collision is structurally impossible after migration and
does not itself need "resolving" — it needs the underlying content sorted into entity kinds, which is
what the rest of this table does.

## 0. Dependency status — the `gate:dependency` label on #6131 is stale

The task that produced this document was asked to confirm `crucible-dashboard-I6122` and
`nousergon-console-I2` are closed. Neither reference resolves as written — both are stale cross-references
from before the 2026-08-02 issue-number migration, already flagged and corrected in #6131's own comment
thread (2026-08-03). Re-verified here:

| Body says | Actually | State |
|---|---|---|
| `nous-ergon-ops-I327` (entity index) | `alpha-engine-config#6122` | **OPEN**, but substantially shipped per its own 2026-08-03 comment: deliverables 1 (typed entity model), 2 (URL addressability), 3 (generated nav) and 5's mechanism (relations) are met; deliverable 4 (search) is met; deliverable 6 (§9.3 reachability) shipped dishonestly and its residual is tracked separately (`nousergon-console-I16`). Recommendation on that thread: close #6122 against `nousergon-console#4`/`#5`, which are the two issues still open. |
| `nousergon-console-I2` (fleet adapters) | `nousergon-console#2` | **CLOSED** 2026-08-02 |
| (also cited on #6131's thread) `nousergon-console-I3` | `nousergon-console#3` | **CLOSED** 2026-08-02 |
| (also cited) `nousergon-console-I14` (§5.8 declared fields — sequencing precondition for the *first migration slice*, not deliverables 1–2) | `nousergon-console#14` | **CLOSED** 2026-08-03 |
| (also cited) `nousergon-console-I11` (§2.6 emission envelope — same precondition) | `nousergon-console#11` | **CLOSED** 2026-08-03 |

**Net:** every blocker the `gate:dependency` label names is closed except `alpha-engine-config#6122`
itself, which is open only pending a coordination call (close it against `#4`/`#5`) rather than any
unshipped capability. Nothing currently blocks deliverables 1–2 (this document), and per the 2026-08-03
comment on #6131, nothing blocked them starting 2026-08-03. `nousergon-console#4` and `#5` remain open and
are the real gate on *deliverable 3* (the migration itself needs the entity index's remaining pieces —
retention-bounded history, faceting — landed), not on this table. The label is left in place per this
task's instructions (do not mutate `gate:*` labels); Brian or the dependency sweep should clear it against
this evidence.

## 1. Entity kind legend (`console-policy.md` §2.1)

| Kind | Definition | Identifier |
|---|---|---|
| Component | Runs unattended, can fail with no human present | `component_id` |
| Run | One execution of one component | run id |
| Cycle | A business period (trading day, weekly cadence, groom cycle, deploy) | cycle id |
| Artifact | A durable produced/consumed thing (S3 key, table, doc) | its key/URI |
| Signal | One named measurement over time with baseline/threshold | metric name |
| Decision | A ruling, gate, queued reserved matter, policy clause, open ASK | tracker ref |
| Incident | A failure record with severity tier and failure-mode class | incident id |
| **ORPHAN** | *Not one of the seven* — no clean entity answer (§9.5) | — |

## 2. Fork merges (deliverable 2)

### Cluster A — the four-way "fleet status" cluster named in the issue

`48_Fleet_Status` / `54_Fleet_SLA` / `53_Status_Generated` / `50_System_State`. Read in full; **not all
four answer the same question** — the issue's cluster conflated a genuine fork with two doc-mirror
orphans that only *look* related by topic.

| View | What it actually answers | Verdict |
|---|---|---|
| `48_Fleet_Status.py` | Which components are online/stalled/offline **right now** (live 30s-polling triage index, schedule-aware dot status) | **Survivor — but structurally subsumed, not migrated as a pane.** This is exactly `console-policy.md` §4.3's default landing view (exception-first, computed from the index, no bespoke code). It needs no migration work beyond verifying the generated landing view covers its schedule-aware dot semantics — flagged as a checklist item on the Pipeline & Fleet Reliability slice (§4 below), not a pane to build. |
| `54_Fleet_SLA.py` | Did each scheduled process hit its SLA, and what's its 30-day hit-rate track record | **Survives as a Signal-kind drill-down tab on the Component detail page**, not a top-level nav pane. Its own docstring already disclaims duplication ("Fleet Status already owns the at-a-glance dots; this page is the SLA-accountability drill-down") — the question is real and distinct, it just isn't top-level. Assigned to the Pipeline & Fleet Reliability slice. |
| `53_Status_Generated.py` | Machine-derived cross-repo state (lib-pin matrix, per-repo HEAD, open PRs), rendered as a pre-built markdown blob from `STATUS_GENERATED.md` | **Loser — retires as a page.** Its content is not itself a question, it's a doc mirror (ORPHAN, §3 below) of facts that already have entity homes: per-repo HEAD and lib-pin state are Component facets once each repo is onboarded with a descriptor, and open PRs are Decision entities the console already indexes. The `STATUS_GENERATED.md` doc-generation job itself can retire once those facets render directly — named here rather than silently dropped, per §9.5. |
| `50_System_State.py` | Operator-authored **prose** invariants and cross-repo arcs, from the hand-maintained `SYSTEM_STATE.md` | **Not a fork loser — a true orphan.** Hand-written narrative doesn't reduce to any of the seven kinds; forcing it into an entity page would either truncate the prose or invent an eighth kind (forbidden, §2.1). Named as a content orphan in §3, not resolved by any survivor. |

**Net for Cluster A:** one survivor is actually free (subsumed by generated nav), one loser survives as a
facet, one loser retires with its facts redistributed to existing entity kinds, and one is a genuine
orphan — not a 4-way fork at all once read closely.

### Cluster B — `23_LLM_Cost` / `36_LLM_Usage`, named in the issue

**Verdict: not a fork.** Read in full, these answer genuinely different questions: `23_LLM_Cost`
("how much are we spending on pay-per-token LLM calls this week, personal non-Anthropic vs. the research
pipeline's Anthropic spend") tracks metered API spend; `36_LLM_Usage` (page title is actually "Plan", not
"LLM Usage") tracks the **flat Claude Max 20x subscription quota** (WET vs. ceiling) — a different billing
mechanism entirely, explicitly excluded from `23`'s scope by `23`'s own docstring. Each page already
cross-references the other as a companion tab in the old dashboard. **Both survive as distinct Signal
panes** on migration; no merge needed. The issue's premise here does not hold up against the file
contents — corrected rather than silently followed.

### Cluster C — newly identified: `Optimizer.py` vs. `{30_Optimizer_Risk.py, 32_Optimizer_Decision.py}`

Not named in the issue; found by comparing declared questions across the table. `Optimizer.py`'s own
docstring says it "consolidates two former pages" into a lens-switcher (Cycle decision / History levers)
over the same `predictor/optimizer_shadow/{date}.json` artifact that `30_Optimizer_Risk.py` (risk levers
over time) and `32_Optimizer_Decision.py` (per-name sizing detail) read independently. Evidence is mixed
on which is current:

- File mtimes: `Optimizer.py` is Jul 23 (older); `30_`/`32_` are Aug 2 (newer) — suggests the numbered
  pair is the more recent split-out.
- Live routing: `host_execution.py` (the entrypoint that's actually wired up) still delegates to
  `Optimizer.py`, **not** to `30_`/`32_` — suggests the split hasn't been cut over in the old dashboard
  itself.

**Recommendation, not asserted as fact:** treat `30_Optimizer_Risk.py` and `32_Optimizer_Decision.py` as
survivors (each answers a distinct question per §4.4's "same-resource, different-question is legitimate"
test, which fits the console's per-question entity-page model better than a lens-switcher), and
`Optimizer.py` as the loser whose question is answered by the union of the two. This needs the Portfolio &
Trading slice (§4 below) to verify against current `host_execution.py` routing before executing — flagged
explicitly as a TBD in the table rather than silently resolved, since the mtime and routing evidence
disagree on which side is "current."

### Lower-confidence overlap flagged, not resolved here

`host_crucible_results.py`'s own docstring says its Overview/Evaluation/Execution tabs were dropped as
"~80-90% duplicates of Report Card / Execution" and now live only on the legacy `/dash` skin. This implies
`Crucible_Evaluation.py`/`Crucible_Execution.py` may substantially overlap `Report_Card.py`/`6_Execution.py`
— but "~80-90%" from a docstring is not a verified fork the way Clusters A and B are. Flagged for the
Evaluation & Backtesting slice to verify at migration time, not merged here.

## 3. Orphans (§9.5) — named explicitly, not dropped

**Content orphans (6)** — no clean entity-kind answer, each needs its own disposition call at migration
time (stay reachable as a plain doc link outside the entity model, or retire):

| View | Why it's an orphan |
|---|---|
| `10_Architecture.py` | Static topic page — hardcoded mermaid diagrams, no per-instance identifier. A topic, not a question (§4.4 forbids exactly this shape for a *pane*). |
| `50_System_State.py` | Hand-authored prose invariants/arcs — see Cluster A above. |
| `51_Architecture_Doc.py` | Static markdown mirror of `ARCHITECTURE.md` (design rationale, not state). |
| `52_Experiments_Log.py` | Static markdown mirror of `EXPERIMENTS.md` (append-only ledger prose). Not to be confused with `46_Experiments.py` (a live Signal pane over scored S3 artifacts — that one is not a doc mirror and is not an orphan). |
| `53_Status_Generated.py` | Doc mirror of `STATUS_GENERATED.md` — see Cluster A above; facts are redistributable, the page itself isn't one entity. |
| `Crucible_Overview.py` | Tear-sheet landing page compositing Evaluation/Validation/Execution/Trust facts at once — a topic ("is the experiment doing well") spanning multiple kinds, not owning one. |

**Navigation wrappers (10)** — `host_agent_reviews.py`, `host_cost_usage.py`, `host_crucible_results.py`,
`host_execution.py`, `host_observability.py`, `host_predictor.py`, `host_reference.py`,
`host_research_signals.py`, `host_system_health.py`, `host_universe_scanner.py`. These are not orphans in
the §9.5 sense — they render nothing themselves, they're thin `st.tabs` routers (confirming the "two
Streamlit apps from one checkout" gotcha in #6131). The console's generated three-tier nav (§4.1) replaces
this entire category structurally; there is no pane to build and no question to preserve, so these are not
carried into the follow-up issues below — they're named here for completeness and covered by the
retirement PR (deliverable 4).

## 4. Full mapping table (deliverable 1)

Disposition legend: **PANE** = migrates as a console pane (a follow-up slice issue owns it) · **FACET** =
migrates as a tab/drill-down on another entity's page, not top-level nav · **SUBSUMED** = needs no
migration work, replaced structurally by the generated index · **ORPHAN** = see §3 · **NAV** = see §3 ·
**FORK-LOSER** = retires, folded into a survivor named in §2.

| # | File | Declared question | Entity kind | Identifier | Data source(s) | Disposition → slice |
|---|---|---|---|---|---|---|
| 1 | `1_Performance.py` | What did the portfolio do (NAV, alpha, attribution, positions) over a selected window, and does the as-of day's EOD report match the emailed version? | Cycle | trading date | `loaders.s3_loader`: eod_report, eod_pnl, trades_full, daily_closes; `shared.attribution`/`position_pnl` | PANE → S1 Portfolio & Trading |
| 2 | `10_Architecture.py` | (topic page, no question) | ORPHAN | — | mostly hardcoded mermaid | ORPHAN (§3) |
| 3 | `11_Signal_Lifecycle.py` | What happened to ticker X on date Y across the whole pipeline, thesis to backtester accuracy? | Signal | (ticker, signal_date) | `loaders.db_loader`, `outcome_store`, `s3_loader`, `signal_loader` | PANE → S2 Research & Signals |
| 4 | `13_Feature_Store.py` | Which pre-computed inference features are fresh/covered, and how has the production feature set drifted from research/training stats? | Artifact | `features/{date}/*.parquet`, `features/registry.json` | `s3_loader` over features/registry/manifest/drift/predictions JSON | PANE → S3 Predictor & Model |
| 5 | `14_RAG_Inventory.py` | How large and fresh is the RAG corpus backing the research agents, by source and ticker? | Artifact | `rag/manifest/{date}.json` | `s3_loader.load_rag_manifest` → `alpha-engine-research/rag/manifest/` | PANE → S8 Research Content & Scheduling |
| 6 | `15_Regime.py` | What regime state does the HMM substrate show this week, does it agree with the macro agent, and is a change signal firing? | Signal | weekly run_id / calendar_date | `s3_loader` regime_substrate/fast_signal/drawdown/retrospective loaders | PANE → S3 Predictor & Model |
| 7 | `16_Order_Book_Rationale.py` | Why is each ticker in the universe in its current order-book state today? | Decision | (ticker, date) | `components.artifact_archive`; `s3_loader.load_order_book_rationale_history` | PANE → S1 Portfolio & Trading |
| 8 | `17_Research_Briefing_Archive.py` | What did the research morning-briefing email say on a given date, vs. recent weeks? | Artifact | `consolidated/{date}/morning.md` | `components.process_archive` | PANE → S8 Research Content & Scheduling |
| 9 | `2_Signals_and_Research.py` | What is today's full signal universe, and how has a selected ticker's score/conviction evolved? | Signal | (ticker, signal_date) | `signal_loader`, `db_loader` (research.db), `outcome_store`, `s3_loader` | PANE → S2 Research & Signals |
| 10 | `23_LLM_Cost.py` | How much are we spending on pay-per-token LLM calls this week, personal vs. research-pipeline? | Signal | (source, date, model) | `s3_loader.load_claude_code_usage`, `load_llm_cost_parquets`; `krepis.usage_pacing` | PANE → S6 Fleet Ops Meta (Cluster B, §2 — NOT a fork of #37) |
| 11 | `25_Pipeline_Status.py` | What is the current run status of each of the three SF pipelines, and did each stage's artifact land? | Run | SF execution ARN/run_id | `nousergon_lib.pipeline_status`; `pipeline_status_loader` (SF API, S3 fallback) | PANE → S5 Pipeline & Fleet Reliability |
| 12 | `26_Artifact_Freshness.py` | Which registered load-bearing S3 artifacts are fresh, stale, missing, or failing their probe? | Artifact | key from `ARTIFACT_REGISTRY.yaml` | `s3_loader._fetch_s3_json` over `_freshness_monitor/*.json`; `ARTIFACT_REGISTRY.yaml` SoT | PANE → S5 Pipeline & Fleet Reliability |
| 13 | `27_Active_Observations.py` | Which observe-mode rollouts are gated-off/on/always-on, and what's each one's cutover gate? | Decision | entry id in `OBSERVATION_REGISTRY.yaml` | `observation_registry_loader` → `OBSERVATION_REGISTRY.yaml` SoT | PANE → S5 Pipeline & Fleet Reliability |
| 14 | `27_Flow_Doctor_Heartbeat.py` | Is each flow's flow-doctor alive/quiet, firing, or silently suppressing errors? | Component | flow name | `s3_loader` over `_flow_doctor/heartbeat/{flow}/{date}.json` | PANE → S5 Pipeline & Fleet Reliability |
| 15 | `28_Retros.py` | Which incidents are ready for a written retro, and which recurring incidents need triage? | Incident | (subsystem, normalized summary) | `s3_loader` → `changelog/retro_candidates.json` | PANE → S7 Incidents & Changelog |
| 16 | `29_Decision_Review.py` | What did the research pipeline decide about ticker X in a cycle, and why (or why not)? | Decision | (ticker, eval_date) | `db_loader` → research.db (scanner/team/cio evaluations) | PANE → S2 Research & Signals |
| 17 | `3_Analysis.py` | Are the system's signals predictive, and how well is the pipeline learning over time? | Run | backtest run_date | `charts.*`, `components.backtester_significance`, `db_loader`, `s3_loader` | PANE → S4 Evaluation & Backtesting (grab-bag spanning Signal/Run/Component — may split further at migration time) |
| 18 | `30_Optimizer_Risk.py` | How have the optimizer's deployed risk levers and the live book's realized risk moved day to day? | Signal | run_date | `s3_loader.load_optimizer_risk_history` → `predictor/optimizer_shadow/{date}.json` | PANE → S1 (Cluster C survivor, §2 — TBD pending routing check) |
| 19 | `31_CIO_Review.py` | What did the CIO committee decide for each candidate ticker, and why? | Decision | (ticker, eval_date) | `signal_loader`, `db_loader` → research.db | PANE → S2 Research & Signals |
| 20 | `32_Optimizer_Decision.py` | Why did the MVO optimizer assign each stock the target weight it did on a given day? | Decision | (ticker, run_date) | `s3_loader` → `predictor/optimizer_shadow/{date}.json` | PANE → S1 (Cluster C survivor, §2 — TBD pending routing check) |
| 21 | `33_Sector_Team_Review.py` | What did one sector team see, how did its analyst rank candidates, what did it recommend to the CIO? | Run | (eval_date, team_id) | research.db team_inputs/team_candidates; S3 sector_team_runs envelope | PANE → S2 Research & Signals |
| 22 | `34_Scanner.py` | How many of the ~900-name universe did the weekly scanner gate pass/fail, and why, by sector? | Run | eval_date | research.db scanner_evaluations; S3 universe-board gate_config | PANE → S2 Research & Signals |
| 23 | `35_Model_Zoo.py` | Which model won this cycle's champion/challenger contest, and how has rotation trended? | Run | (leaderboard date, version_id) | S3 `predictor/model_zoo/leaderboard/{date}.json`; research.db predictor_outcomes | PANE → S3 Predictor & Model |
| 24 | `36_LLM_Usage.py` | How much of the flat Claude Max 20x subscription quota has been used, and cache efficiency? | Signal | (source, date) | S3 `claude_code_usage/{source}/{date}.json`; `config/usage_pacing.json` | PANE → S6 Fleet Ops Meta (Cluster B, §2 — NOT a fork of #10) |
| 25 | `36_Predictor_Training.py` | Did this cycle's base-retrain pass the IC gate and get promoted? | Run | (run_date, model_version) | S3 `predictor/metrics/training_summary_{date}.json` | PANE → S3 Predictor & Model |
| 26 | `37_Watch_Status.py` | What did the SF Watch / CI Watch agents do the last time a run failed, and how well is watch performing? | Incident | (watch-log date, event_id) | S3 `saturday_sf_watch/`, `ci_watch/` consolidated logs | PANE → S6 Fleet Ops Meta |
| 27 | `38_Changelog.py` | What production failures happened fleet-wide in the lookback window, and what are the most frequent error signatures? | Incident | event_id | S3 `changelog/entries/` event lake | PANE → S7 Incidents & Changelog |
| 28 | `39_Universe_Board.py` | How attractive is each of the ~900 scanner-universe stocks right now, and did it pass the gate? | Artifact | ticker | S3 `scanner/universe/latest.json` | PANE → S2 Research & Signals |
| 29 | `40_Attractiveness_Trends.py` | Which stocks' attractiveness is trending up, and which haven't repriced yet? | Signal | (ticker, as_of) | S3 `scanner/universe/trajectory/latest.json`; attractiveness-history parquet | PANE → S2 Research & Signals |
| 30 | `41_Quarantine.py` | Which changelog entries were rejected by vocab validation, and why? | Incident | (event_id, day) | S3 `changelog/quarantine/` | PANE → S7 Incidents & Changelog |
| 31 | `42_Backlog_Groom.py` | Did the groom actually engage each queued backlog issue this run, and is the backlog draining? | Run | `groom/{date}/{run_id}.json` | S3 groom run artifacts, decisions, audits | PANE → S6 Fleet Ops Meta |
| 32 | `43_Distillation_Corpus.py` | How close is the SFT distillation corpus to the ~1000-pair kill-gate trigger? | Signal | corpus_stats snapshot date | S3 `decision_artifacts/distillation/corpus_stats/latest.json` | PANE → S8 Research Content & Scheduling |
| 33 | `44_Think_Tank.py` | How does the daily think tank's independent rating compare to the scanner's attractiveness score? | Signal | (ticker, trading_day) | S3 `thinktank/ratings/`, `thinktank/thesis/`, `thinktank/themes/` | PANE → S8 Research Content & Scheduling |
| 34 | `45_Morning_Signal_Schedule.py` | What is the morning-signal podcast scheduled to air on a given day, and did it air? | Cycle | calendar date | S3 `morning-signal-podcast/schedule/`; `schedule/applied/` markers | PANE → S8 Research Content & Scheduling |
| 35 | `46_Experiments.py` | How do champion-loop challengers perform against the live champion, and which arm is live? | Signal | (spec/arm, cohort_date) | S3 `signals_shadow/`, `candidates_shadow/`, `producer_leaderboard/`, champion pointer | PANE → S3 Predictor & Model |
| 36 | `47_Merged_PRs.py` | Which PRs merged across the fleet recently, human or agent? | Artifact | `org/repo#N` | GitHub Search API; S3 `ops/pr_merge_attribution/latest.json` | PANE → S6 Fleet Ops Meta |
| 37 | `48_Fleet_Status.py` | Which fleet components are online, stalled, or offline right now? | Component | component_id | EC2/SSM, SF status, S3 freshness/groom/health markers | SUBSUMED (§2 Cluster A) — no pane, verify §4.3 landing view covers dot semantics, checklist item on S5 |
| 38 | `49_Decision_Queue.py` | Which open issues/PRs are waiting on Brian's ruling, oldest first? | Decision | `repo#N` | GitHub Issues/PRs via `decision_queue_loader` | PANE → S6 Fleet Ops Meta |
| 39 | `5_Focus_List.py` | Is the regime-blended factor-composite focus list actually predicting which names the quant agent picks? | Signal | (eval_date, focus_team_id) | `db_loader` (research.db) | PANE → S2 Research & Signals |
| 40 | `50_Data_Integrity.py` | Has any L1 cross-source market-value disagreement been observed, and which tickers/cells are flagged? | Signal | (phase, ticker) | `data_integrity_loader.gather_data_integrity_signals` | PANE → S5 Pipeline & Fleet Reliability |
| 41 | `50_Expenses.py` | Which external providers are trending over/under their monthly budget, and what will each cost by month-end? | Signal | (provider_key, billing period) | `s3_loader` → S3 `expenses/latest.json`; `config/expense_budgets.json` | PANE → S6 Fleet Ops Meta |
| 42 | `50_System_State.py` | (prose invariants, no computed question) | ORPHAN | — | `system_docs_loader` → `SYSTEM_STATE.md` | ORPHAN (§2 Cluster A, §3) |
| 43 | `51_Architecture_Doc.py` | Why is the system shaped the way it is (design rationale)? | ORPHAN | — | `system_docs_loader` → `ARCHITECTURE.md` | ORPHAN (§3) |
| 44 | `52_Experiments_Log.py` | What has been tried, including negative results, and what was learned? | ORPHAN | — | `system_docs_loader` → `EXPERIMENTS.md` | ORPHAN (§3) |
| 45 | `53_Status_Generated.py` | What is the machine-derived cross-repo state as of the last daily regeneration? | ORPHAN | — | `system_docs_loader` → `STATUS_GENERATED.md` (GHA-generated) | ORPHAN / FORK-LOSER (§2 Cluster A, §3) |
| 46 | `54_Fleet_SLA.py` | Did each scheduled process complete within SLA, and what's its 30-day hit-rate? | Component (facet) | process_id | `sla_status_loader.gather_sla_inputs` — same freshness-monitor planes as #12 | FACET → S5 Pipeline & Fleet Reliability (Cluster A, §2) |
| 47 | `54_PR_Pipeline.py` | How are PR-sweep cycles trending over 14 days — bucket sizes, merge throughput, gate verdicts? | Cycle | groom run key | `pr_merge_loader` (live GitHub API); `s3_loader` groom sweep artifacts | PANE → S6 Fleet Ops Meta |
| 48 | `55_Universe_Churn.py` | How much does each universe cut turn over week to week, and which names persist? | Artifact | `universe_membership/{date}/membership.json` | `s3_loader.load_universe_membership_history` | PANE → S2 Research & Signals |
| 49 | `6_Execution.py` | What trades were executed, and how did fills/slippage compare to intended price? | Run | (trade_id/ticker, created_at) | `s3_loader.load_trades_full`; `db_loader.get_score_performance`; `outcome_store` | PANE → S1 Portfolio & Trading |
| 50 | `7_Predictor.py` | Is the live predictor model healthy, and did the most recent training run get promoted? | Component | component_id=predictor | `s3_loader` predictor metrics/training-state/production-health | PANE → S3 Predictor & Model |
| 51 | `8_Eval_Quality.py` | Are LLM-agent outputs holding rubric-scored quality over time, per agent/criterion, and did a prompt bump move scores? | Signal | (agent_id, criterion, judge_model) | `eval_loader` → S3 `decision_artifacts/_eval/` | PANE → S4 Evaluation & Backtesting |
| 52 | `Crucible_Evaluation.py` | For a selected experiment tile, what is each metric's value/CI/target/trend/pass-fail, and why? | Signal | metric key within tile | `s3_loader.load_report_card` → `evaluator/{date}/report_card.json` | PANE → S4 (flag: possible overlap w/ Report_Card — verify at migration, §2) |
| 53 | `Crucible_Execution.py` | For a selected backtest run, how good were entry/exit timing, and did the risk guard add value? | Artifact | (backtest_date, filename) | `s3_loader` → `backtest/{date}/trigger_scorecard.json` etc. | PANE → S4 Evaluation & Backtesting |
| 54 | `Crucible_Feedback.py` | For each auto-apply optimizer loop, was this week's parameter change promoted, blocked, or gated — why? | Decision | (optimizer_loop_id, week) | `s3_loader.load_apply_audit`, `load_autoapply_config_meta` | PANE → S4 Evaluation & Backtesting |
| 55 | `Crucible_Overview.py` | At a glance, is the Reference Rate experiment performing well, and is measurement trustworthy? | ORPHAN | — | `s3_loader.load_report_card`/`load_eod_pnl`; `trust_battery_loader` | ORPHAN (§3) |
| 56 | `Crucible_Trust.py` | Which named validation-battery checks pass live CI, and what real defects has the battery caught historically? | Incident | (battery leg, finding row) | `trust_battery_loader.load_ci_verdicts` (live GH Actions); hardcoded `BATTERY_LEGS`/`BATTERY_FINDINGS` | PANE → S4 Evaluation & Backtesting |
| 57 | `Crucible_Validation.py` | For a selected backtest run, do integrity checks pass, and which sub-scores predict the 21-day outcome? | Signal | (check name, backtest_date) | `s3_loader` → `backtest/{date}/pit_parity.json` etc. | PANE → S4 Evaluation & Backtesting |
| 58 | `Daily_News.py` | What news stories, sentiment, and event flags fired today for the held+tracked universe? | Artifact | (run_id/date) | S3 `data/news_articles_daily/{run_id}_articles.parquet` | PANE → S8 Research Content & Scheduling |
| 59 | `Data_and_Maturity.py` | Is each optimizer's feedback loop accruing enough data to promote, and was its live artifact actually written? | Signal | (optimizer name, accrual metric) | `db_loader.load_research_db`; `outcome_store`; `s3_loader` manifest counts | PANE → S5 Pipeline & Fleet Reliability |
| 60 | `Director_Plan.py` | What did the weekly Director advisory pass propose, and what carried over from last week? | Decision | run date → `action_plan.json` items | `s3_loader` → `director/{date}/action_plan.json`, `carryover_ledger.json` | PANE → S8 Research Content & Scheduling |
| 61 | `Fleet_Checks.py` | Which scheduled fleet checks are healthy, failing, or gone stale/silent? | Component | check_id | `fleet_checks_loader.load_check_results` → `ops/checks/{check_id}/latest.json` | PANE → S5 (canonical `ops/checks/<id>/latest.json` envelope instance — migrate intact per #6131's gotcha) |
| 62 | `host_agent_reviews.py` | (routing only) | ORPHAN | — | delegates to #16, #21, #19 | NAV (§3) |
| 63 | `host_cost_usage.py` | (routing only) | ORPHAN | — | delegates to #41, #10, #24 | NAV (§3) |
| 64 | `host_crucible_results.py` | (routing only) | ORPHAN | — | delegates to #57, #54, #56 | NAV (§3) — flags possible Report_Card overlap, see §2 |
| 65 | `host_execution.py` | (routing only) | ORPHAN | — | delegates to #7, #49, `Optimizer.py` | NAV (§3) — routing evidence for Cluster C, §2 |
| 66 | `host_observability.py` | (routing only) | ORPHAN | — | delegates to #12, #61, #13, Incidents, #59 | NAV (§3) |
| 67 | `host_predictor.py` | (routing only) | ORPHAN | — | delegates to #6, #4, Predictor_Archives | NAV (§3) |
| 68 | `host_reference.py` | (routing only) | ORPHAN | — | delegates to #2, #3, #5, #42, #43, #44, #45 | NAV (§3) |
| 69 | `host_research_signals.py` | (routing only) | ORPHAN | — | delegates to #9, #58, #8 | NAV (§3) |
| 70 | `host_system_health.py` | (routing only) | ORPHAN | — | delegates to #26, #31, #36, #47, #14 | NAV (§3) — pinned deep-link asserted by `tests/test_fleet_status_page.py`, migration must preserve URL or update test |
| 71 | `host_universe_scanner.py` | (routing only) | ORPHAN | — | delegates to #28, #22, #29, #48, #39 | NAV (§3) |
| 72 | `Incidents.py` | Across the changelog corpus, what failed — raw events, retros, or quarantine rejects? | Incident | (event id) | delegates (lazily) to #27, #15, #30 | PANE → S7 Incidents & Changelog |
| 73 | `Optimizer.py` | What did the portfolio optimizer decide per stock this cycle, and how have deployed risk levers evolved? | Decision | date → optimizer_shadow record | `predictor/optimizer_shadow/{date}.json` (same as #18/#20) | FORK-LOSER → S1 (Cluster C, §2 — TBD, verify against `host_execution.py` routing before retiring) |
| 74 | `Predictor_Archives.py` | What historical predictor prediction runs and weekly training summaries have been produced? | Artifact | date | `components.process_archive`; S3 `predictor/predictions/`, `predictor/metrics/training_summary_` | PANE → S3 Predictor & Model |
| 75 | `Report_Card.py` | What is the current institutional grade (RED/WATCH/GREEN) for each module/component, and why? | Signal | component/module within `report_card.json` | `s3_loader.load_report_card`; `components.report_card_v2` | PANE → S4 Evaluation & Backtesting (flag: possible overlap w/ Crucible_Evaluation — verify at migration, §2) |

## 5. Proposed domain slices for deliverable 3 (staged as follow-up issues)

58 of 75 views need actual pane-migration work (6 content orphans + 10 nav wrappers + 1 subsumed view
excluded, per §3/§2). Grouped into 8 domain slices, each ≤10 views, plus one orphans issue and one
retirement-PR issue — filed as `nousergon-console` issues, listed with numbers in the closing PR/comment
on #6131.

| Slice | Views | Count | Resolves |
|---|---|---|---|
| S1 Portfolio & Trading | `1_Performance`, `6_Execution`, `16_Order_Book_Rationale`, `30_Optimizer_Risk`, `32_Optimizer_Decision`, `Optimizer.py` (TBD) | 6 | Cluster C (§2) |
| S2 Research & Signals | `2_Signals_and_Research`, `11_Signal_Lifecycle`, `29_Decision_Review`, `31_CIO_Review`, `33_Sector_Team_Review`, `34_Scanner`, `39_Universe_Board`, `40_Attractiveness_Trends`, `5_Focus_List`, `55_Universe_Churn` | 10 | — |
| S3 Predictor & Model Lifecycle | `7_Predictor`, `13_Feature_Store`, `15_Regime`, `35_Model_Zoo`, `36_Predictor_Training`, `46_Experiments`, `Predictor_Archives` | 7 | — |
| S4 Evaluation & Backtesting | `3_Analysis`, `8_Eval_Quality`, `Crucible_Evaluation`, `Crucible_Execution`, `Crucible_Feedback`, `Crucible_Trust`, `Crucible_Validation`, `Report_Card` | 8 | possible Report_Card/Crucible_Evaluation overlap (§2) |
| S5 Pipeline & Fleet Reliability | `25_Pipeline_Status`, `26_Artifact_Freshness`, `27_Active_Observations`, `27_Flow_Doctor_Heartbeat`, `Fleet_Checks`, `50_Data_Integrity`, `54_Fleet_SLA`, `Data_and_Maturity` | 8 | Cluster A (§2) — also verifies `48_Fleet_Status` subsumption |
| S6 Fleet Ops Meta | `23_LLM_Cost`, `36_LLM_Usage`, `37_Watch_Status`, `42_Backlog_Groom`, `47_Merged_PRs`, `49_Decision_Queue`, `50_Expenses`, `54_PR_Pipeline` | 8 | Cluster B (§2) |
| S7 Incidents & Changelog | `28_Retros`, `38_Changelog`, `41_Quarantine`, `Incidents.py` | 4 | — |
| S8 Research Content & Scheduling | `14_RAG_Inventory`, `17_Research_Briefing_Archive`, `43_Distillation_Corpus`, `44_Think_Tank`, `45_Morning_Signal_Schedule`, `Daily_News`, `Director_Plan` | 7 | — |

Orphans issue: the 6 content orphans in §3 (disposition call per view — stay reachable as a plain doc
link, or retire). Retirement PR issue: deliverable 4, depends on all 8 slices + the orphans issue closing.

## 6. Follow-up issues filed

All filed in `nousergon-console`, referencing this PR (#53):

| # | Issue | Views | Resolves |
|---|---|---|---|
| [#54](https://github.com/nousergon/nousergon-console/issues/54) | Portfolio & Trading slice | 6 (S1) | Cluster C fork (§2) |
| [#55](https://github.com/nousergon/nousergon-console/issues/55) | Research & Signals slice | 10 (S2) | — |
| [#56](https://github.com/nousergon/nousergon-console/issues/56) | Predictor & Model Lifecycle slice | 7 (S3) | — |
| [#57](https://github.com/nousergon/nousergon-console/issues/57) | Evaluation & Backtesting slice | 8 (S4) | possible Report_Card overlap |
| [#58](https://github.com/nousergon/nousergon-console/issues/58) | Pipeline & Fleet Reliability slice | 8 (S5) | Cluster A fork (§2) |
| [#59](https://github.com/nousergon/nousergon-console/issues/59) | Fleet Ops Meta slice | 8 (S6) | Cluster B non-fork (§2) |
| [#60](https://github.com/nousergon/nousergon-console/issues/60) | Incidents & Changelog slice | 4 (S7) | — |
| [#61](https://github.com/nousergon/nousergon-console/issues/61) | Research Content & Scheduling slice | 7 (S8) | — |
| [#62](https://github.com/nousergon/nousergon-console/issues/62) | Orphan disposition | 6 content orphans | §3 |
| [#63](https://github.com/nousergon/nousergon-console/issues/63) | Retirement PR (deliverable 4) | all 75 | gated on #54–#62 |

## 7. Orphan disposition (issue #62) — final calls

Each of the 6 content orphans from §3, read in full (not just their docstring) and disposed per
§9.5 ("named explicitly, never silently dropped"). None resolves as a console pane — each is
confirmed, on closer reading, to be genuinely outside the seven entity kinds (§2.1); none is
recategorized. No pane code accompanies this PR because none of the 6 gets a pane.

| View | Disposition | Reachability after retirement | Mechanism |
|---|---|---|---|
| `10_Architecture.py` | **Retire.** Static topic page (hardcoded mermaid + module cards), no per-instance identifier — confirmed against §4.4's "topic, not a question" test. The page's own docstring/footer already treats itself as disposable: it points readers at `OVERVIEW.md` / `nousergon-docs` "so it doesn't drift out of sync," per config#1989's ruling to delete hand-kept prose in favor of a pointer. | `nousergon-docs` README / `OVERVIEW.md` (public repo, GitHub-native markdown rendering) | Zero new mechanism — the pointer this page already uses is the disposition. |
| `50_System_State.py` | **Retire.** Hand-authored prose invariants/arcs. Doesn't reduce to any of the seven kinds; forcing it into an entity page truncates the prose or invents an eighth kind (forbidden, §2.1). | `alpha-engine-config/private-docs/SYSTEM_STATE.md` (+ `system_state/*.md`), viewed via GitHub's native markdown rendering of the private repo | Zero new mechanism — already reachable today by anyone with repo access; nothing changes. |
| `51_Architecture_Doc.py` | **Retire.** Static markdown mirror of design rationale, not state. | `alpha-engine-config/private-docs/ARCHITECTURE.md`, GitHub-native rendering | Zero new mechanism. |
| `52_Experiments_Log.py` | **Retire.** Static markdown mirror, append-only ledger prose (distinct from `46_Experiments.py`, a live Signal pane — confirmed not conflated). | `alpha-engine-config/private-docs/EXPERIMENTS.md`, GitHub-native rendering | Zero new mechanism. |
| `53_Status_Generated.py` | **Retire the page now; do not retire the generator.** Confirmed live consumers of `STATUS_GENERATED.md` beyond the dashboard: `alpha-engine-config/AGENTS.md` documents it as the derived-state source of truth, `.github/groom-conflict-resolve-prompt.md` names it explicitly, and `SYSTEM_STATE_changelog.md` shows it regenerated as a routine step of nearly every session wind-down. Retiring `regenerate-status.yml` today would break those. Content is redistributable — per-repo HEAD/lib-pin state becomes a Component facet once each repo carries a §2.6 descriptor; open PRs are already Decision entities the console indexes (`49_Decision_Queue.py` → S6 pane, `47_Merged_PRs.py` → S6 pane). | Short-term: `alpha-engine-config/private-docs/STATUS_GENERATED.md`, GitHub-native rendering (same as the three docs above). Long-term: Component/Decision facets on the console, once descriptor rollout covers the repos it currently summarizes. | Short-term: zero new mechanism. Long-term: tracked as [`alpha-engine-config-I6800`](https://github.com/nousergon/alpha-engine-config/issues/6800) — decompose-and-retire, gated on descriptor coverage, not actionable yet. |
| `Crucible_Overview.py` | **Retire, no replacement pane.** Not a doc mirror — a tear-sheet landing page compositing Evaluation/Validation/Execution/Trust facts at once. Its constituent facts are already assigned to panes in the Evaluation & Backtesting slice (issue #57 — `Report_Card.py`, `Crucible_Trust.py`, `Crucible_Validation.py`, `Crucible_Evaluation.py`). A bespoke cross-linking landing view is not worth building on top of that: `console-policy.md` §4.3 already mandates an exception-first landing view (everything not `HEALTHY`, transparency-gap count, decision queue, completeness ratio) which serves the "at a glance, is it doing well" need structurally, without a per-experiment narrative tear-sheet — and adding a second landing-style pane is exactly what §4.4 forbids ("same-question, different-place is a fork"). | Individual facts: the S4 panes named above, plus §4.3's generated landing view for the aggregate "is anything wrong" question. | Zero new mechanism — §4.3 already covers this; no gap. |

**Orphan count (§9 item 5) re-measured:** 0 panes silently dropped — all 6 have an explicit, recorded disposition above. 5 of 6 retire outright with reachability preserved by the owning repo's existing GitHub rendering (nothing new built). 1 (`53_Status_Generated.py`) retires as a *page* now while its *generator* stays live, pending a tracked future decomposition.

### Policy gap flagged, not resolved here

`console-policy.md` has **no carve-out** for "content that must stay reachable but is not a fact
about any of the seven entity kinds" — confirmed by reading §2.1, §3.5, and §4.1–§4.5 in full.
§3.5 forbids a hand-maintained nav entry; §2.1 forbids an eighth kind; §4.1's three tiers
(Overview → Domain → Entity) are generated from the registry, with no room for a hand-placed
static link. The disposition above works around this cleanly for all 6 cases — each source doc
already lives in a repo with its own native GitHub rendering, so "stays reachable" costs zero
console-side mechanism — but that is a favorable accident of *these* 6 orphans (each has an
existing home with a public or private GitHub repo behind it), not a structural answer. A future
orphan without an existing repo-native home would have nowhere to go. Filed as
[`alpha-engine-config-I6801`](https://github.com/nousergon/alpha-engine-config/issues/6801) for Brian to rule whether this needs a narrow policy
amendment or an explicit "permanently out of scope" ruling.

### TODO — orphans flagged by sibling slices after this PR

None as of this PR's opening (2026-08-10) — `gh pr list --search "docs migration"` shows only #53
(the mapping table itself, merged) matching that search; the 8 domain-slice PRs (#54–#61) were
still in flight and none had posted an orphan flag in their PR body at the time this was written.
**If a sibling slice PR (#54–#61) surfaces an additional orphan — a view that looked
entity-shaped in the mapping table but wasn't, once its builder read the source — add it to the
table above with the same disposition columns, as a follow-up commit on this branch/PR rather
than a new issue, per this issue's own instructions.** Re-check before merging:
`gh pr list --repo nousergon/nousergon-console --search "docs migration" --state all` and read
each PR body's own findings section.

## 8. Cluster C fork resolved (§54)

`host_execution.py`'s live routing still delegates to `Optimizer.py` (confirmed against
`crucible-dashboard`'s own `tests/test_host_execution_wiring.py::test_optimizer_tabs_removed_from_eval_host`,
which pins `30_Optimizer_Risk.py`/`32_Optimizer_Decision.py` as retired FROM the eval host and asserts
`host_execution.py`'s tab list is `[Order Book, Execution, Optimizer]` — not the split pair). `Optimizer.py`
itself imports and `_exec_view`s `30_`/`32_` as two lenses (`st.segmented_control`), so the mtime-vs-routing
disagreement §2/Cluster C flagged resolves as: **`Optimizer.py` wins** — the numbered files are its
lens implementations, not independent survivors. S1 migrates the Portfolio & Trading slice as **5** panes,
not 6: `1_Performance`, `6_Execution`, `16_Order_Book_Rationale`, and one merged Optimizer Decision-kind
pane carrying both the per-ticker sizing lens and the per-day risk-lever lens (the latter as a Signal-kind
entity on the same artifact, since a lever time-series and a per-ticker decision are genuinely different
questions per §4.4, even sourced from one key). See `nousergon-console#54`'s closing PR for the adapter
config and row-contract verification.

## 9. S4 Evaluation & Backtesting slice resolved (#57)

Migrated **5 of 8** views as `s3-records` panes (config instances in `config.example.yaml`, all
`enabled: false`): `crucible-report-card` (merged, see below), `crucible-execution` (+
`crucible-execution-blotter` for the CSV order blotter), `crucible-validation` (+
`crucible-validation-signal-quality` for the CSV per-signal table), `crucible-feedback` (+
`crucible-feedback-configs`), `eval-quality`. Extended `s3-records`'s `records_path` with a `*`-wildcard
grouped-fan-out mode (`group_field`) to reach a report card's per-tile nested dict-then-array and an
apply-audit's dict-of-loops shape — neither a plain array nor parallel arrays could reach either.

**Report_Card / Crucible_Evaluation overlap (flagged in #57) resolved: merge.** Both read
`evaluator/{date}/report_card.json` and carry the same MetricRecord fields (`value`/`ci_low`/`ci_high`/
`n_samples`/`target`/`red_line`/`trend_decoration`/`criticality`/`status`/`status_reason`) — the former's
module-rollup framing and the latter's per-tile metric-browser framing are the SAME question rendered two
ways, which §4.4 treats as a fork ("same-question, different-place"). The console's generic Signal list
(facetable by the injected `tile` field) plus each Signal's own entity page serve both framings with one
adapter instance and zero bespoke rendering.

**Deferred, each with a follow-up issue:**

- `Crucible_Trust.py` — both halves (live CI verdicts, historical findings ledger) depend on
  `results/battery_registry.py`, a Python list literal in `crucible-dashboard`'s own source, not a
  registry shape any driver/adapter here can read without either importing that repo as a dependency or
  hand-transcribing its content (forbidden by §2.4/§7). `nousergon-console#72`.
- `3_Analysis.py` — a three-former-page merge (Signal Accuracy / Backtester / Pipeline Eval) spanning a
  live SQLite database (`loaders.db_loader`), five-plus S3 artifact families and two chart-derivation
  modules computing statistics rather than passing through raw fields — genuinely unresolved which entity
  kind each tab becomes, not a scoped implementation gap. `nousergon-console#74` (complexity:high).

§13 population-completeness: these are adapter-projected Signal/Artifact/Decision entities, not
Components, so §9.1 (registry-bound) is unaffected by this slice; §9.5's orphan count and §7's per-registry
index-page count both drop by the domains covered here once an instance is enabled against real infra.

---
Prepared by: Claude Sonnet 5 via [Claude Code](https://claude.com/claude-code)
