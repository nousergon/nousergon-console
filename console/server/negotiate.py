"""Content negotiation — one URL, two representations (§3.8).

Two ways to ask for JSON, and the order they are tried in is the whole design:

1. **`Accept: application/json`.** Primary, unambiguous, works on every route.
2. **A `.json` suffix on the path.** A fallback, for pasting into a browser or
   a `curl` with no flags — and it is a fallback rather than the primary
   because **entity identifiers legitimately end in `.json`.** An artifact's id
   *is* its object key (§3.2), so `/artifact/ops/checks/x/latest.json` names a
   real entity, and a naive suffix rule would strip it and 404.

So the suffix is only ever applied to a path that does **not** already resolve.
`negotiate()` takes a `resolves` predicate rather than the index itself, so the
rule is testable without building a graph and so this module knows nothing
about entities.
"""
from __future__ import annotations

from typing import Callable

#: Media types that mean "give me the machine-readable representation".
JSON_TYPES = ("application/json", "application/*", "+json")

SUFFIX = ".json"


def wants_json(accept_header: str | None) -> bool:
    """Whether the Accept header asks for JSON.

    `*/*` deliberately does NOT count. A browser sends it, a `curl` with no
    flags sends it, and defaulting those to JSON would make the human surface
    unreachable by the exact readers §4.2's interaction budget is measured on.
    """
    if not accept_header:
        return False
    header = accept_header.lower()
    return any(t in header for t in JSON_TYPES)


def negotiate(
    path: str,
    accept_header: str | None,
    resolves: Callable[[str], bool],
    routable: Callable[[str], bool] | None = None,
) -> tuple[str, bool]:
    """Return the path to resolve and whether to render JSON.

    ``resolves`` answers "does this path name something that EXISTS" — a known
    route whose entity, if any, is present. It is consulted BEFORE the suffix
    is considered, which is what keeps an artifact whose key ends in `.json`
    addressable at its own identifier.

    ``routable`` answers the weaker "is this a known route at all", and it is
    what makes a **404 answer in the representation that was asked for**. A
    consumer who appended `.json` and got back an HTML error page has to parse
    prose to learn that their component does not exist — which is exactly the
    failure the JSON representation exists to remove, arriving on the one code
    path nobody exercises until something is already wrong. So the suffix is
    an explicit JSON request whether or not the target turns out to be there.
    """
    json_requested = wants_json(accept_header)
    if resolves(path):
        return path, json_requested
    if not path.endswith(SUFFIX):
        return path, json_requested

    stripped = path[: -len(SUFFIX)] or "/"
    if resolves(stripped) or (routable is not None and routable(stripped)):
        # The suffix wins over an Accept header that said nothing — a pasted
        # `.json` URL means JSON. Where the route is known but the entity is
        # absent, the stripped path is still the right one to resolve: the
        # 404 then names the entity the consumer meant, not the spelling they
        # used to ask for JSON.
        return stripped, True
    # Not a route at all. Hand the ORIGINAL back so the 404 names what was
    # asked for rather than something the server rewrote it into — but answer
    # it as JSON, because that is what the suffix requested.
    return path, True
