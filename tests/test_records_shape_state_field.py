"""`state_field` as an ordered list of paths: the first non-null value wins.

The list form exists for a source that publishes its own resolved disposition
beside its raw verdict (alpha-engine-config-I12859): a monitor that writes
`console_state: DISABLED` on a row a recorded ruling declared off, and
`state: stale` on every row. Read through `state` alone, the declared-off row
renders as a stale exception on the landing view although the source already
said it is off. A single string must behave exactly as before.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from console.model.kinds import Kind, State
from console.records_shape import resolve_state
from console.render.html import is_exception

NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
BOTH = ["console_state", "state"]


def _resolve(kind, state_field, root, state_map=None, state_default=None):
    return resolve_state(kind, state_field, state_default, state_map, root,
                         None, None, 2.0, NOW)


def test_list_prefers_the_first_present_path():
    root = {"state": "stale", "console_state": "DISABLED"}
    assert _resolve(Kind.ARTIFACT, BOTH, root) == "DISABLED"


def test_list_falls_back_when_the_first_path_is_absent_or_null():
    assert _resolve(Kind.ARTIFACT, BOTH, {"state": "stale"}) == "stale"
    assert _resolve(Kind.ARTIFACT, BOTH, {"state": "stale", "console_state": None}) == "stale"


def test_a_string_behaves_exactly_as_before():
    root = {"state": "stale", "console_state": "DISABLED"}
    assert _resolve(Kind.ARTIFACT, "state", root) == "stale"
    assert _resolve(Kind.ARTIFACT, "console_state", root) == "DISABLED"


def test_dotted_paths_work_inside_the_list():
    root = {"verdict": {"resolved": None, "raw": "stale"}}
    assert _resolve(Kind.ARTIFACT, ["verdict.resolved", "verdict.raw"], root) == "stale"


def test_nothing_present_still_uses_the_default_then_unreported():
    assert _resolve(Kind.ARTIFACT, BOTH, {}, state_default="unobserved") == "unobserved"
    assert _resolve(Kind.COMPONENT, BOTH, {}) is State.UNREPORTED
    assert _resolve(Kind.COMPONENT, [], {"state": "HEALTHY"}) is State.UNREPORTED


def test_component_kinds_still_map_and_flag_typos():
    root = {"console_state": None, "state": "passed"}
    assert _resolve(Kind.COMPONENT, BOTH, root, {"passed": "HEALTHY"}) is State.HEALTHY
    assert _resolve(Kind.COMPONENT, BOTH, {"state": "nonsense"}) is State.DEGRADED


def test_the_declared_off_row_leaves_the_exception_view_and_a_live_stale_row_stays():
    def ent(root):
        # `is_exception` reads only `state`; the rest of an Entity is noise here.
        return SimpleNamespace(kind=Kind.ARTIFACT, state=_resolve(Kind.ARTIFACT, BOTH, root))
    assert not is_exception(ent({"state": "stale", "console_state": "DISABLED"}))
    assert is_exception(ent({"state": "stale"}))
