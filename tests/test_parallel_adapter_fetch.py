"""Adapter reads run concurrently but fold into the index in declared order
(alpha-engine-config-I13625: serial reads put the first build at ~1460s)."""
from __future__ import annotations

import threading
import time
from typing import Callable

import pytest

from console import config as cfg_mod


def test_fetch_all_is_concurrent_and_ordered() -> None:
    barrier = threading.Barrier(4, timeout=5)

    def make(i: int) -> Callable[[dict], int]:
        def fetch(_cfg: dict) -> int:
            barrier.wait()  # only passes if all four run at once
            time.sleep(0.01 * (4 - i))  # finish out of order
            return i
        return fetch

    out = cfg_mod._fetch_all([(make(i), {}) for i in range(4)])
    assert [r for r, _ in out] == [0, 1, 2, 3]
    assert all(elapsed >= 0 for _, elapsed in out)


def test_fetch_all_propagates_first_failure_in_order() -> None:
    def ok(_c: dict) -> int:
        return 1

    def boom(_c: dict) -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        cfg_mod._fetch_all([(ok, {}), (boom, {}), (ok, {})])


def test_fetch_all_empty_and_single() -> None:
    assert cfg_mod._fetch_all([]) == []
    (r, _), = cfg_mod._fetch_all([(lambda c: 7, {})])
    assert r == 7
