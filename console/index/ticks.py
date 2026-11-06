"""Tick lists: a checkbox beside each row of a declared table, saved to S3.

Brian, 2026-11-05, on `/page/reading-list`: "we should also have check boxes
next to each item so i can mark when they are read". A TICK LIST is declared
once in config:

    ticks:
      reading-list:
        store: s3://alpha-engine-research/dashboard/reading-list/reads.json
        label: read

and a page `table` section opts in with ``tick: {list: reading-list, key:
item_id}``: each row gets a checkbox, keyed by that row's ``item_id`` field.

**Why this is a second write, and where the fact lives.** The console is a
read-only index; its first write (`index/actions.py`) puts a tick on the
issue, because that is where an action already lives. A "read" mark has no
such home yet, so the declared ``store`` IS its home: one JSON object,
``{"schema_version": 1, "list": <name>, "marks": {<key>: {"done", "at",
"by"}}}``, that the list's own producer can read back (the reading-list job
reads it to show what has been read). The console keeps nothing on the box:
the store is read on every build, and a POST re-reads it before writing so a
second writer is never clobbered by a stale copy.

Each bound is checked here rather than trusted to the caller:

- only a declared list can be written, and only to its declared object;
- only a key that a table declaring that list is SHOWING can be ticked, so
  the endpoint cannot write arbitrary keys;
- a key is a short plain identifier, and a list holds at most `MAX_MARKS`.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass

SOURCE_NAME = "ticks"
SCHEMA_VERSION = 1
_ATTR = "_console_ticks"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
_STORE = re.compile(r"^s3://(?P<bucket>[a-z0-9][a-z0-9.-]{1,62})/(?P<key>[A-Za-z0-9/._-]{1,512}\.json)$")
#: The most marks one list keeps; a tick past it is refused, never truncated.
MAX_MARKS = 5000

#: (bucket, key) -> the object's bytes, or None when it does not exist yet.
Getter = Callable[[str, str], "bytes | None"]
#: (bucket, key, body) -> None.
Putter = Callable[[str, str, bytes], None]


class TicksConfigError(ValueError):
    """A `ticks:` block this build cannot honour, named by key."""


@dataclass(frozen=True)
class Declaration:
    name: str
    bucket: str
    key: str
    label: str = "done"

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


@dataclass
class Store:
    """What the last build (or the last tick) read for one list."""

    declaration: Declaration
    marks: dict[str, dict[str, Any]] = field(default_factory=dict)
    read_at: str | None = None
    error: str | None = None

    def done(self, key: str) -> bool:
        return bool((self.marks.get(key) or {}).get("done"))


# ------------------------------------------------------------------ parse ---

def parse(raw: Any) -> dict[str, Declaration]:
    """The `ticks:` block as {name: Declaration}; empty when there is none."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TicksConfigError("ticks: must be a mapping of list name to {store, label}")
    out: dict[str, Declaration] = {}
    for name, spec in raw.items():
        name = str(name)
        where = f"ticks.{name}"
        if not _NAME.match(name):
            raise TicksConfigError(f"{where}: name must be lowercase letters, digits and dashes")
        if not isinstance(spec, Mapping):
            raise TicksConfigError(f"{where}: must be a mapping with `store`")
        unknown = sorted(set(spec) - {"store", "label"})
        if unknown:
            raise TicksConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")
        m = _STORE.match(str(spec.get("store") or ""))
        if m is None:
            raise TicksConfigError(f"{where}.store must be s3://<bucket>/<key>.json")
        label = spec.get("label", "done")
        if not isinstance(label, str) or not label.strip() or len(label) > 20:
            raise TicksConfigError(f"{where}.label must be a short word")
        out[name] = Declaration(name=name, bucket=m["bucket"], key=m["key"],
                                label=label.strip())
    return out


# ------------------------------------------------------------------- store --

def _s3_get(bucket: str, key: str) -> bytes | None:
    from ..aws import client as _client

    s3 = _client("s3")
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 - a missing object is an empty list
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404", "NotFound"} or type(exc).__name__ == "NoSuchKey":
            return None
        raise


def _s3_put(bucket: str, key: str, body: bytes) -> None:
    from ..aws import client as _client

    _client("s3").put_object(Bucket=bucket, Key=key, Body=body,
                             ContentType="application/json")


def _marks(body: bytes | None, decl: Declaration) -> dict[str, dict[str, Any]]:
    if body is None:
        return {}
    doc = json.loads(body.decode("utf-8"))
    marks = doc.get("marks") if isinstance(doc, Mapping) else None
    if not isinstance(marks, Mapping):
        raise ValueError(f"{decl.uri} has no `marks` mapping")
    return {str(k): dict(v) for k, v in marks.items() if isinstance(v, Mapping)}


def read(decls: Mapping[str, Declaration], now: str,
         getter: Getter | None = None) -> tuple[dict[str, Store], list[AdapterResult]]:
    """Every declared list's marks. A list whose store cannot be read is a
    FAILED source with its reason, and renders unticked and read-only."""
    get = getter or _s3_get
    stores: dict[str, Store] = {}
    errors = []
    for name, decl in decls.items():
        try:
            stores[name] = Store(decl, _marks(get(decl.bucket, decl.key), decl), read_at=now)
        except Exception as exc:  # noqa: BLE001 - shown as a failed source
            stores[name] = Store(decl, read_at=now, error=f"{type(exc).__name__}: {str(exc)[:160]}")
            errors.append(f"{name}: {stores[name].error}")
    status = AdapterStatus.FAILED if errors else AdapterStatus.OK
    result = AdapterResult(name=SOURCE_NAME, status=status, claim_class=ClaimClass.OBSERVATION,
                           fetched_at=now, unavailable=tuple(errors))
    return stores, [result] if decls else []


def attach(index: Any, stores: dict[str, Store]) -> None:
    setattr(index, _ATTR, stores)


def store(index: Any, name: str) -> Store | None:
    return (getattr(index, _ATTR, None) or {}).get(name)


# ------------------------------------------------------------------ write ---

#: One lock per store object. `tick` is a read-modify-write of the WHOLE
#: object and the server is a ThreadingHTTPServer, so two ticks landing
#: together each read the same marks and the later put erased the earlier
#: mark (alpha-engine-config-I13239, codex 23e92b8f13727a91 / 8ed39846f7376189).
#: Serialising the read-put pair per object closes that inside this process.
_STORE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _store_lock(bucket: str, key: str) -> threading.Lock:
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault((bucket, key), threading.Lock())


class TickRefused(Exception):
    """A tick the handler will not save, with the reason and HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def tick(index: Any, name: str, key: str, done: bool, actor: str | None, now: str,
         allowed: set[str], getter: Getter | None = None,
         putter: Putter | None = None) -> Store:
    """Mark (``done``) or unmark one SHOWN key on a declared list. Re-reads the
    store first, writes it whole, and reflects it in the index the page reads."""
    found = store(index, name)
    if found is None:
        raise TickRefused(f"no tick list {name!r} is declared on this console", 404)
    if not KEY.match(key) or key not in allowed:
        raise TickRefused(f"{key!r} is not a row shown with a {name} checkbox", 404)
    decl = found.declaration
    with _store_lock(decl.bucket, decl.key):
        try:
            marks = _marks((getter or _s3_get)(decl.bucket, decl.key), decl)
        except Exception as exc:  # noqa: BLE001 - nothing was written
            raise TickRefused(f"could not read {decl.uri}: {str(exc)[:160]}", 502) from None
        if done:
            marks[key] = {"done": True, "at": now, "by": actor}
        else:
            marks.pop(key, None)
        if len(marks) > MAX_MARKS:
            raise TickRefused(f"{name} already holds {MAX_MARKS} marks", 409)
        doc = {"schema_version": SCHEMA_VERSION, "list": name, "updated_at": now, "marks": marks}
        try:
            from ..render.json import dumps  # the one wire encoder (test_wire_encoder)

            (putter or _s3_put)(decl.bucket, decl.key, dumps(doc).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - nothing was written
            raise TickRefused(f"could not save {decl.uri}: {str(exc)[:160]}", 502) from None
        found.marks, found.read_at, found.error = marks, now, None
    return found
