"""git-host adapter — Decision, Incident and Artifact entities from a Git
host's trackers.

Issues become Decision entities; anything carrying the configured incident
label becomes an Incident (`config.example.yaml`'s `git-host` block). The
identifier is the tracker ref the host assigns — `<repo>-I<N>` — never
console-minted (§2.1).

**Pull requests, when `include_prs` is set (`nousergon-console#59`).** An
open PR becomes a Decision at `<repo>-PR<N>` — a separate namespace from
issues so a repo's issue #12 and PR #12 never collide — answering "what is
waiting on Brian's ruling" alongside open issues. A **merged** PR becomes an
**Artifact** at `<org>/<repo>#<N>` — "which PRs merged, human or agent" is a
fact about a durable produced thing, not an open ruling, so it takes the
kind whose identifier the issue itself declares (`org/repo#N`). A closed,
unmerged PR answers neither question and is not emitted. `include_prs`
defaults false: every existing deployment of this adapter reads issues only
until it opts in, so enabling PR support never changes what an unchanged
config emits.

The host API shape is known to this adapter and nothing else (§2.3). All
topology — org, repo list, the incident label — comes from configuration.

The adapter is hermetic: the network calls are two injectable functions,
``lister`` (issues) and ``pr_lister`` (pull requests), so tests run over
recorded fixtures with no live host (the ``groom-sweep-policy.md`` §8.1
standard). In production both shell to the host's CLI (`gh`), which is
configuration-resolved, not a hardcoded endpoint.

**Structural leaf for `consumed-by` (§3.3/§6, `nousergon-console#52`).** A
Decision, Incident or a merged-PR Artifact is a terminal record — it is read
by a human making a ruling or auditing what shipped, not consumed by another
fleet component with a declarable id. The host API this adapter reads
(`gh issue list` / `gh pr list`) carries only number, title, tracker state,
labels and timestamps: no field names a consuming component, and §2.3 forbids
minting one by reaching into another adapter's output. Unlike
`checks_envelope`/`yaml_directory`/`object_store`/`state_machine`, there is no
config or source field here to declare a consumer from — this is not an
oversight, it is the adapter genuinely having nothing to declare.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Iterable

from ..model.entity import Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind

#: A tracker is an OBSERVATION (§2.5): it reports the current state of issues
#: and pull requests, which is a fact about them at a moment in time.
CLAIM_CLASS = ClaimClass.OBSERVATION

name = "repos"
produces = ("decision", "incident", "artifact")

#: A lister takes (org, repo) and returns the raw issue/PR dicts for one repo.
#: Injectable for hermetic tests; the default hits the host via `gh`.
Lister = Callable[[str, str], list[dict[str, Any]]]


def _gh_lister(org: str, repo: str) -> list[dict[str, Any]]:
    out = subprocess.run(
        [
            "gh", "issue", "list", "--repo", f"{org}/{repo}",
            "--state", "all", "--limit", "200",
            "--json", "number,title,state,labels,updatedAt,url",
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout or "[]")


def _gh_pr_lister(org: str, repo: str) -> list[dict[str, Any]]:
    out = subprocess.run(
        [
            "gh", "pr", "list", "--repo", f"{org}/{repo}",
            "--state", "all", "--limit", "200",
            "--json", "number,title,state,labels,updatedAt,url,mergedAt",
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout or "[]")


def fetch(
    config: dict[str, Any],
    lister: Lister | None = None,
    pr_lister: Lister | None = None,
) -> AdapterResult:
    org = config.get("org")
    repos: Iterable[str] = config.get("repos") or []
    incident_label = config.get("incident_label", "incident")
    include_prs = bool(config.get("include_prs"))
    if not org:
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            fetched_at=now_iso(),
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("all",),
        )
    lister = lister or _gh_lister
    pr_lister = pr_lister or _gh_pr_lister

    entities: list[Entity] = []
    unavailable: list[str] = []
    failed = False
    for repo in repos:
        try:
            items = lister(org, repo)
        except Exception:  # host unreachable for this repo → FAILED, not empty
            failed = True
            continue
        for item in items:
            entities.append(_to_entity(org, repo, item, incident_label))

        if not include_prs:
            continue
        try:
            prs = pr_lister(org, repo)
        except Exception:  # a PR-search failure never drops the issue claims already read
            failed = True
            continue
        for pr in prs:
            entity = _pr_to_entity(org, repo, pr)
            if entity is not None:
                entities.append(entity)

    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.FAILED if failed and not entities else AdapterStatus.OK,
        entities=tuple(entities),
        unavailable=tuple(unavailable),
    )


def _to_entity(
    org: str, repo: str, item: dict[str, Any], incident_label: str
) -> Entity:
    labels = {l.get("name", "") for l in item.get("labels", [])}
    number = item.get("number")
    is_incident = incident_label in labels
    kind = Kind.INCIDENT if is_incident else Kind.DECISION
    ref = f"{repo}-I{number}"  # tracker ref, the source-assigned id (§2.1)
    state = _state(item, is_incident)
    return Entity(
        kind=kind,
        id=ref,
        state=state,
        provenance=Provenance(
            source=f"{org}/{repo}",
            as_of=item.get("updatedAt"),
            evidence=item.get("url"),
        ),
        facets={"repo": f"{org}/{repo}"},
        detail={
            "title": item.get("title", ""),
            "tracker_state": item.get("state", ""),
            "labels": sorted(labels),
        },
    )


def _state(item: dict[str, Any], is_incident: bool) -> str:
    """A Decision or an Incident is not a component, so §5.1's second half
    applies and the row carries the tracker's own value.

    An issue is open or closed. It is not HEALTHY, and it is certainly not
    FAILED — mapping it into observability-policy.md §8.3's component
    vocabulary was what forced an open decision to render `UNKNOWN`, the escape
    hatch §8.3 forbids by name. Open decisions belong on the "waiting on Brian"
    half of the landing view (§4.3), not in the transparency-gap count, whose
    objective is zero and which an open backlog would inflate on sight."""
    tracker_state = (item.get("state") or "").strip().lower()
    if not tracker_state:
        return "unreported-by-tracker"
    if is_incident:
        return "open-incident" if tracker_state == "open" else "resolved"
    return tracker_state


def _pr_to_entity(org: str, repo: str, item: dict[str, Any]) -> Entity | None:
    """A merged PR is an Artifact (`org/repo#N`, `nousergon-console#59`'s
    47_Merged_PRs question: which PRs merged, human or agent). An open PR is
    a Decision (`<repo>-PR<N>`, a distinct namespace from `<repo>-I<N>` so an
    issue and a PR sharing a number never collide) — it joins issues on the
    "waiting on Brian" queue. A closed, unmerged PR answers neither question
    and is intentionally dropped here rather than carried as a third,
    unused shape."""
    number = item.get("number")
    tracker_state = (item.get("state") or "").strip().lower()
    merged_at = item.get("mergedAt")

    if merged_at:
        return Entity(
            kind=Kind.ARTIFACT,
            id=f"{org}/{repo}#{number}",
            state="merged",
            provenance=Provenance(
                source=f"{org}/{repo}",
                as_of=str(merged_at),
                evidence=item.get("url"),
            ),
            facets={"repo": f"{org}/{repo}"},
            detail={
                "title": item.get("title", ""),
                "merged_at": merged_at,
            },
        )
    if tracker_state == "open":
        labels = {l.get("name", "") for l in item.get("labels", [])}
        return Entity(
            kind=Kind.DECISION,
            id=f"{repo}-PR{number}",
            state="open",
            provenance=Provenance(
                source=f"{org}/{repo}",
                as_of=item.get("updatedAt"),
                evidence=item.get("url"),
            ),
            facets={"repo": f"{org}/{repo}"},
            detail={
                "title": item.get("title", ""),
                "tracker_state": tracker_state,
                "labels": sorted(labels),
                "is_pull_request": True,
            },
        )
    return None  # closed, unmerged — neither declared question names this row
