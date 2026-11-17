# Actions docket implementation plan

Goal: render the Decisions section as a grouped docket with explicit batch submission.
Architecture: extend Action with decision-only body projections; retain option letters/text as the ruling contract. Group in first-occurrence priority order, with Other last. Reuse copy blocks and the existing tick flow, including per-item failure feedback. No server write API changes.
Spec: the six layout requirements supplied for this change; scope tracked in alpha-engine-config-I13764.
Constraints: unchanged steps, agents, checkboxes and ruling readback; CSS variables only; 390px and both themes; no baseline growth.

- [x] Parser tests then implementation: first matching Due/Re-exam/Default line, four relative date classes, What this is/fallback paragraph, tagged option labels, stable grouping.
- [x] Render tests then implementation: three decisions/two projects, chips, option cards and initial bar text; factor existing command markup without changing output for steps.
- [x] Selection and submission tests then script: selections remain local, only exceptions shown, one tick per decision, prevent repeat submissions of confirmed items, preserve unsaved picks across refresh.
- [x] Run full pytest, workflow lint/type gates, inspect mobile/light/dark layout and review diff.
- Delivery: commit, push `feat/actions-docket-layout`, open a ready PR and verify CI.

Review focus: missing recommendation must require an explicit pick; partial failure retries only unsaved items; auto-refresh must not discard unsubmitted choices; malformed dates must not crash; arbitrary issue text must remain escaped.

Verification ledger: parser/render/JavaScript unit tests went red before implementation and now pass. Chromium at 390px in light/dark verified local-only selection, all-pick submission, failed-item retry and no repeated confirmed writes. Independent review found bold date whitespace and stale dirty state after agent handoff; both fixed with red-to-green regression tests. No deferred findings.

Assumptions: project groups follow first occurrence in the existing priority order; absent recommendations require an explicit pick. Invalid calendar dates render no chip. These choices avoid inventing priorities or rulings.

Final local verification: 2,143 passed, 1 skipped; coverage 93.49%. basedpyright 0 errors/warnings, baseline unchanged. F821 and changed-source ANN lint pass. Example index build and namespace check pass.
