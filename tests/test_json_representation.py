"""Same URL, two representations (§3.8) — the chokepoint for CN-3.8.

The defect: `grep -rn 'application/json' console/` returned **nothing**. Every
route served `text/html` and there was no other representation. The only
machine-readable projection was `console index`, a CLI dump — not addressable,
not the UI's query, and unreachable by anything that is not a shell on the host.

The fleet's own agents read this surface. One they cannot read forces every
agent to re-derive fleet state from raw sources, which is §2.4's second
inventory one layer up — and the agent's picture and the operator's picture
then diverge **exactly when something is wrong**, because that is when the
derivations differ.

`test_every_route_serves_both_representations` is the clause. It **enumerates**
routes rather than listing them, because a hand-listed test misses the next
route somebody adds — which is the same defect one layer up.
"""
from __future__ import annotations

import json

import pytest

from console.index.graph import Index
from console.model.entity import Edge, Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.server.negotiate import negotiate, wants_json
from console.server.router import resolve


@pytest.fixture
def index() -> Index:
    idx = Index()
    idx.add_result(AdapterResult(
        name="fixture", status=AdapterStatus.OK,
        claim_class=ClaimClass.OBSERVATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-alpha", state=State.HEALTHY,
                   provenance=Provenance("checks", "2026-08-03T12:00:00Z",
                                         "https://e/1"),
                   facets={"owner": "brian"},
                   detail={"fields": {"rows": {"value": 903, "unit": "rows",
                                               "baseline": 900,
                                               "render": "count"}}}),
            Entity(kind=Kind.COMPONENT, id="comp-broken", state=State.FAILED,
                   provenance=Provenance("checks", "2026-08-03T12:00:00Z")),
            # An artifact whose identifier ENDS IN .json — the case the naive
            # suffix rule breaks, and the reason negotiation asks the index first.
            Entity(kind=Kind.ARTIFACT, id="ops/checks/comp-alpha/latest.json",
                   state="fresh", provenance=Provenance("s3://b/")),
        ),
        edges=(Edge(source="comp-alpha", rel="produces",
                    target="ops/checks/comp-alpha/latest.json"),),
    ))
    return idx


def _payload(index: Index, path: str, query: str = "") -> dict:
    return render_json.payload(index, resolve(path, query))


# ------------------------------------------------------------ the clause ---

def test_every_route_serves_both_representations(index):
    """Enumerated, not listed. A hand-listed test misses the next route added,
    which is the same defect this clause exists to prevent one layer up."""
    from console.server.app import _html

    routes = ["/", "/search?q=comp"] + [f"/{k.route}" for k in Kind]
    routes += [e.route for e in index.all()]

    for route in routes:
        path, _, query = route.partition("?")
        req = resolve(path, query)
        doc = render_json.payload(index, req)
        assert doc["schema_version"] == render_json.SCHEMA_VERSION, route
        assert doc["view"] == req.view, route
        html = _html(index, req)
        assert html.startswith("<!doctype html>"), route


def test_both_representations_come_from_the_same_resolved_request(index):
    """One router, one query. The two cannot drift in coverage because neither
    has a route table of its own — this asserts the shape that guarantees it."""
    from console.server.app import _html

    req = resolve("/component", "owner=brian")
    doc = render_json.payload(index, req)
    html = _html(index, req)
    # The same filter applied, from the same Resolved, on both sides.
    assert doc["showing"] == 1 and doc["of"] == 2
    assert "showing 1 of 2" in html


# -------------------------------------------------------- negotiation ------

def test_accept_header_is_the_primary_signal():
    assert wants_json("application/json")
    assert wants_json("application/vnd.api+json")
    assert not wants_json("text/html")
    assert not wants_json(None)


def test_star_slash_star_does_not_mean_json():
    """A browser sends it and so does a bare `curl`. Defaulting those to JSON
    would make the human surface unreachable by exactly the readers §4.2's
    interaction budget is measured on."""
    assert not wants_json("*/*")
    assert not wants_json("text/html,application/xhtml+xml,*/*;q=0.8")


def test_an_entity_whose_id_ends_in_json_stays_addressable(index):
    """The case a naive suffix rule breaks. An artifact's id IS its object key
    (§3.2), so `/artifact/…/latest.json` names a real entity — and stripping
    the suffix would 404 the very entities the fleet has most of."""
    path = "/artifact/ops/checks/comp-alpha/latest.json"
    resolved, as_json = negotiate(path, None, lambda p: _exists(index, p))
    assert resolved == path and as_json is False
    assert _payload(index, resolved)["entity"]["id"] == \
        "ops/checks/comp-alpha/latest.json"


def test_the_suffix_applies_only_when_the_path_does_not_resolve(index):
    """`/component/comp-alpha.json` is not an entity, so the suffix is tried."""
    resolved, as_json = negotiate("/component/comp-alpha.json", None,
                                  lambda p: _exists(index, p))
    assert resolved == "/component/comp-alpha" and as_json is True


def test_a_doubled_suffix_reaches_the_json_of_a_dot_json_entity(index):
    path = "/artifact/ops/checks/comp-alpha/latest.json.json"
    resolved, as_json = negotiate(path, None, lambda p: _exists(index, p))
    assert resolved == "/artifact/ops/checks/comp-alpha/latest.json"
    assert as_json is True


def test_the_suffix_works_on_list_and_landing(index):
    for asked, expected in (("/component.json", "/component"), ("/.json", "/")):
        resolved, as_json = negotiate(asked, None, lambda p: _exists(index, p))
        assert (resolved, as_json) == (expected, True), asked


def test_an_unroutable_path_is_handed_back_unrewritten_but_still_json(index):
    """The 404 names what was asked for, not something the server rewrote it
    into — a consumer debugging a bad URL needs to see their own URL. It is
    still answered as JSON, because the suffix asked for JSON."""
    resolved, as_json = negotiate("/widgets/nope.json", None,
                                  lambda p: _exists(index, p), _routable_only)
    assert resolved == "/widgets/nope.json" and as_json is True


def test_a_404_answers_in_the_representation_that_was_asked_for(index):
    """A known route with a MISSING entity. Caught by a live HTTP smoke test,
    not by unit tests: `/component/nope.json` returned an HTML error page,
    so a consumer had to parse prose to learn their component did not exist —
    the exact failure the JSON representation removes, arriving on the one
    code path nobody exercises until something is already wrong."""
    resolved, as_json = negotiate("/component/nope.json", None,
                                  lambda p: _exists(index, p), _routable_only)
    # Resolved to the STRIPPED path, so the 404 names the entity the consumer
    # meant rather than the spelling they used to ask for JSON.
    assert resolved == "/component/nope" and as_json is True


def _routable_only(path: str) -> bool:
    from console.server.app import _routable

    return _routable(path, "")


# ------------------------------------------------------------- payloads ----

def test_every_payload_carries_a_schema_version(index):
    for route in ("/", "/component", "/component/comp-alpha", "/search"):
        assert "schema_version" in _payload(index, route, "q=x")


def test_the_landing_payload_is_exception_first_with_denominators(index):
    doc = _payload(index, "/")
    assert [e["id"] for e in doc["exceptions"]] == ["comp-broken"]
    # §5.3: every roll-up states its denominator inline, on the wire exactly as
    # on the page. A consumer reading a bare count is doing what a reader does
    # with a top-10 that does not say it is a top-10.
    assert doc["numbers"]["not_healthy"] == {"count": 1, "of": 3}
    assert doc["numbers"]["transparency_gap"]["of"] == 3


def test_a_list_payload_states_showing_and_of(index):
    doc = _payload(index, "/component", "owner=brian")
    assert (doc["showing"], doc["of"]) == (1, 2)


def test_an_entity_payload_carries_relations_in_both_directions(index):
    """§3.3's reverse edge is the load-bearing one, and an agent asking 'who
    breaks if this is stale' needs it here rather than deriving it."""
    art = _payload(index, "/artifact/ops/checks/comp-alpha/latest.json")
    rels = {(r["rel"], r["target"]) for r in art["relations"]}
    assert ("produced-by", "comp-alpha") in rels


def test_an_entity_payload_carries_its_full_provenance(index):
    doc = _payload(index, "/component/comp-alpha")["entity"]
    assert doc["provenance"] == {
        "source": "checks", "as_of": "2026-08-03T12:00:00Z",
        "evidence": "https://e/1",
    }
    assert doc["url"] == "/component/comp-alpha"


def test_declared_fields_reach_the_wire(index):
    doc = _payload(index, "/component/comp-alpha")["entity"]
    assert doc["detail"]["fields"]["rows"]["unit"] == "rows"


def test_merged_claim_provenance_reaches_the_wire():
    """A consumer that has to ask 'which adapter said this' is back to
    re-deriving, which is the whole thing §3.8 exists to stop."""
    idx = Index()
    for name, cls, state in (("registry", ClaimClass.DECLARATION, State.UNREPORTED),
                             ("checks", ClaimClass.OBSERVATION, State.HEALTHY)):
        idx.add_result(AdapterResult(
            name=name, status=AdapterStatus.OK, claim_class=cls,
            entities=(Entity(kind=Kind.COMPONENT, id="c", state=state,
                             provenance=Provenance(name)),)))
    doc = render_json.entity(idx.entity("c"))
    assert doc["field_sources"]["state"]["source"] == "checks"
    assert any(s["source"] == "registry" for s in doc["superseded"])


def test_a_conflict_reaches_the_wire():
    idx = Index()
    for name, state in (("a", State.HEALTHY), ("b", State.FAILED)):
        idx.add_result(AdapterResult(
            name=name, status=AdapterStatus.OK,
            claim_class=ClaimClass.OBSERVATION,
            entities=(Entity(kind=Kind.COMPONENT, id="c", state=state,
                             provenance=Provenance(name)),)))
    assert render_json.entity(idx.entity("c"))["conflicts"] == ["state"]


def test_the_search_payload_ranks_exact_first(index):
    doc = _payload(index, "/search", "q=comp-alpha")
    assert doc["hits"][0]["exact"] and doc["hits"][0]["entity"]["id"] == "comp-alpha"


def test_the_cli_dump_and_the_http_json_share_one_serializer(index):
    """A CLI dump with its own entity shape is a second wire format, and the
    two would answer the same question differently the first time either
    changed."""
    from console.__main__ import _dump

    dumped = json.loads(_dump(index))
    served = _payload(index, "/component/comp-alpha")["entity"]
    assert next(e for e in dumped["entities"] if e["id"] == "comp-alpha") == served


def _exists(index: Index, path: str) -> bool:
    from console.server.app import _resolves

    return _resolves(index, path, "")
