"""The URL router — `console-policy.md` §3.2 made structural.

Every addressable state has a URL built from entity identifiers, and a pasted
URL reproduces the view exactly on a cold load with no prior client state.
That is enforced here by making the route the *only* input to resolution: the
router is a pure function from a path + query string to a resolved request —
there is no session, no client memory, no hidden state to consult.

Routes:

- ``/<kind>/<id>``            — an entity page (§3.2). Kind segments are the
                                literal kind names, never positional ids.
- ``/<kind>?facet=v&...``     — a filtered list; facets are query parameters
                                so every list state is itself a URL (§3.4).
- ``/``                       — the exception-first landing view (§4.3).
- ``/search?q=...``           — the global identifier resolver (§3.7).
- ``/doctor/<id>``            — why an identifier is or is not on the
                                surface (§3.9). Addressable, because a
                                diagnosis nobody can link to has to be re-run
                                by whoever is asked about it.

The router *resolves*; it does not render. Rendering is the render layer's
job, so resolution is independently testable and the round-trip property is
asserted without any HTML.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl

from ..model.kinds import FACETS, Kind


@dataclass(frozen=True)
class Resolved:
    """A parsed request, fully determined by its URL.

    ``view`` is one of ``landing | entity | list | search``. ``kind``/``id``
    identify the entity for entity and list views; ``facets`` carries the
    active facet filters for list views; ``query`` carries the search string.
    """

    view: str
    kind: Kind | None = None
    entity_id: str | None = None
    facets: dict[str, str] = field(default_factory=dict)
    query: str | None = None


class UnknownRoute(Exception):
    """A path that names no view — surfaced as a 404, never a blank page."""


def resolve(path: str, query_string: str = "") -> Resolved:
    """Resolve a URL path + query string to a Resolved request.

    Pure: the same (path, query_string) always yields the same Resolved, which
    is exactly the round-trip property §3.2 requires — a cold load with this
    URL reconstructs the view with nothing else.
    """
    segments = [s for s in path.split("/") if s != ""]
    params = dict(parse_qsl(query_string, keep_blank_values=True))

    if not segments:
        return Resolved(view="landing")

    if segments[0] == "search":
        return Resolved(view="search", query=params.get("q", ""))

    if segments[0] == "doctor":
        # §3.9. Addressable like everything else — a diagnosis nobody can link
        # to is a diagnosis that has to be re-run by whoever is asked about it.
        # The identifier may be a path segment or `?q=`; both round-trip.
        rest = path.split("/", 2)[2] if len(segments) > 1 else params.get("q", "")
        return Resolved(view="doctor", query=rest)

    kind = Kind.from_route(segments[0])
    if kind is None:
        raise UnknownRoute(f"no view named by segment {segments[0]!r}")

    if len(segments) == 1:
        # A list view: /<kind>?facet=value&...
        facets = {k: v for k, v in params.items() if k in FACETS}
        return Resolved(view="list", kind=kind, facets=facets)

    # An entity page: /<kind>/<id>. The id is everything after the kind
    # segment, NOT a single segment — identifiers legitimately contain slashes
    # (an artifact's id is its object key, e.g. s3://bucket/k.json), and §3.2
    # requires the identifier to be the URL verbatim. There is no route past
    # the entity (§4.1), so the remainder is always the id.
    entity_id = path.split("/", 2)[2]
    return Resolved(view="entity", kind=kind, entity_id=entity_id)


def path_for_entity(kind: Kind, entity_id: str) -> str:
    """The canonical entity URL — `/<kind>/<id>`. One namespace (§3.6)."""
    return f"/{kind.route}/{entity_id}"


def path_for_list(kind: Kind, facets: dict[str, str] | None = None) -> str:
    """The canonical list URL with facets as query parameters (§3.4)."""
    base = f"/{kind.route}"
    if not facets:
        return base
    from urllib.parse import urlencode

    return f"{base}?{urlencode(sorted(facets.items()))}"
