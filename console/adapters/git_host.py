"""git-host adapter — Decision and Incident entities from a Git host's trackers.

Issues and pull requests become Decision entities; anything carrying the
configured incident label becomes an Incident (`config.example.yaml`'s
`git-host` block). The identifier is the tracker ref the host assigns —
`<repo>-I<N>` / `<repo>-PR<N>` — never console-minted (§2.1).

The host API shape is known to this adapter and nothing else (§2.3). All
topology — org, repo list, the incident label — comes from configuration.

The adapter is hermetic: the network call is one injectable function,
``_lister``, so tests run over recorded fixtures with no live host (the
``groom-sweep-policy.md`` §8.1 standard). In production ``_lister`` shells to
the host's CLI (`gh`), which is configuration-resolved, not a hardcoded
endpoint.
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
produces = ("decision", "incident")

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


def fetch(
    config: dict[str, Any],
    lister: Lister | None = None,
) -> AdapterResult:
    org = config.get("org")
    repos: Iterable[str] = config.get("repos") or []
    incident_label = config.get("incident_label", "incident")
    if not org:
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            fetched_at=now_iso(),
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("all",),
        )
    lister = lister or _gh_lister

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
