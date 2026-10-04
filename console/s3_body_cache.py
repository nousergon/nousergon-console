"""Reuse an S3 object's body while its LIST entry says it has not changed.

WHY THIS EXISTS
---------------
The index rebuilds every ``refresh_seconds`` (180 s on the fleet), and every
rebuild re-downloaded every body under every listed prefix — ~228 GETs a pass,
~110k a day, for objects most of which change a few times a day. The adapters
that read bodies (``checks-envelope``, ``s3-records``) already LIST the prefix
first, and a LIST entry carries each key's ETag. An unchanged ETag means an
unchanged body, so the previous pass's bytes are the current bytes and the GET
buys nothing (`alpha-engine-config-I11792`).

THE THREE RULES THAT KEEP IT HONEST
-----------------------------------
- **Only a LIST can make a body reusable.** ``read`` serves cached bytes only
  when the most recent listing of that key carried the SAME tag the bytes were
  stored under. A key that was never listed is always fetched.
- **A key gone from the listing is gone from the cache.** ``observe_listing``
  drops every cached entry under the listed prefix that the listing no longer
  names, so a deleted object can never be rendered from memory (§5.5 — absence
  renders as itself).
- **Process memory only.** Nothing is persisted; a restart starts cold. The
  console renders, it never owns (CONTRIBUTING) — this is a transport saving,
  not a store.

Raw bytes are cached, not decoded bodies: each caller decodes its own copy, so
no adapter can mutate what the next pass reads.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

#: (raw body bytes, the tag S3 returned with them — or None when it gave none)
Fetched = tuple[bytes, "str | None"]


def listing_tag(obj: dict[str, Any]) -> str | None:
    """The change token for one ``list_objects_v2`` ``Contents`` entry.

    ETag when present (it changes with the content); otherwise LastModified
    plus Size, which changes on every rewrite. ``None`` means the entry says
    nothing usable, and the key is then always fetched.
    """
    etag = obj.get("ETag")
    if etag:
        return f"etag:{etag}"
    lm, size = obj.get("LastModified"), obj.get("Size")
    if lm is None or size is None:
        return None
    stamp = lm.isoformat() if hasattr(lm, "isoformat") else str(lm)
    return f"lm:{stamp}|{size}"


def response_tag(resp: dict[str, Any]) -> str | None:
    """The same token, read off a ``get_object`` response.

    Stored with the bytes instead of the LISTED tag so that an object rewritten
    between the LIST and the GET is filed under its NEW tag — the next listing
    then matches it, rather than the new bytes masquerading as the old version.
    """
    etag = resp.get("ETag")
    if etag:
        return f"etag:{etag}"
    lm, size = resp.get("LastModified"), resp.get("ContentLength")
    if lm is None or size is None:
        return None
    stamp = lm.isoformat() if hasattr(lm, "isoformat") else str(lm)
    return f"lm:{stamp}|{size}"


class ListedBodyCache:
    """Bodies keyed by (bucket, key), valid only against the latest listing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listed: dict[tuple[str, str], str | None] = {}
        self._bodies: dict[tuple[str, str], tuple[str, bytes]] = {}

    def observe_listing(self, bucket: str, prefix: str,
                        tags: dict[str, str | None]) -> None:
        """Record one COMPLETE listing of ``prefix``; forget what it omits."""
        with self._lock:
            for store in (self._listed, self._bodies):
                for b, k in [bk for bk in store
                             if bk[0] == bucket and bk[1].startswith(prefix)
                             and bk[1] not in tags]:
                    del store[(b, k)]
            for key, tag in tags.items():
                self._listed[(bucket, key)] = tag

    def read(self, bucket: str, key: str, fetch: Callable[[], Fetched]) -> bytes:
        """Cached bytes when the latest listed tag matches them, else ``fetch()``."""
        bk = (bucket, key)
        with self._lock:
            listed = self._listed.get(bk)
            hit = self._bodies.get(bk)
        if listed is not None and hit is not None and hit[0] == listed:
            return hit[1]
        raw, tag = fetch()
        with self._lock:
            if tag is not None and bk in self._listed:
                self._bodies[bk] = (tag, raw)
            else:
                self._bodies.pop(bk, None)
        return raw

    def clear(self) -> None:
        """Drop everything. For tests."""
        with self._lock:
            self._listed.clear()
            self._bodies.clear()


#: One per process — shared by every body-reading adapter, keyed by bucket.
BODY_CACHE = ListedBodyCache()
