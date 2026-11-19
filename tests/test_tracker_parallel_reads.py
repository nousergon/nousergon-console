"""Bounded parallel tracker reads retain ordering and failure visibility."""
import threading
import time
from console.adapters import git_host


def test_named_reads_overlap_but_preserve_order():
    barrier = threading.Barrier(3, timeout=2)
    def get(path):
        number = int(path.rsplit("/", 1)[-1])
        barrier.wait()
        time.sleep((4 - number) / 100)
        return {"number": number, "state": "closed"}
    rows = git_host._read_named(get, "org", "repo", [1, 2, 3], "test")
    assert [row["number"] for row in rows] == [1, 2, 3]


def test_epic_reads_overlap_and_fail_in_declared_order():
    barrier = threading.Barrier(3, timeout=2)
    def reader(org, repo, number, cache):
        barrier.wait()
        if number == 2:
            raise RuntimeError("source unavailable")
        return [{"number": number, "state": "open", "title": str(number)}]
    rows, result = git_host.fetch_sub_issues(
        {"org": "org"}, ["repo-I3", "repo-I2", "repo-I1"], reader)
    assert list(rows) == ["repo-I3", "repo-I1"]
    assert result.unavailable == ("repo-I2 (RuntimeError: source unavailable)",)


def test_rest_epics_share_one_token_per_repo(monkeypatch):
    minted = []
    monkeypatch.setattr(git_host, "_REST_CACHE", {})
    def mint(*args):
        minted.append(args)
        time.sleep(0.02)
        return "test-token"
    monkeypatch.setattr(git_host, "_mint_token", mint)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b"[]"
    reader = git_host.rest_sub_issue_reader(["helper"], urlopen=lambda *a, **k: Response())
    rows, result = git_host.fetch_sub_issues(
        {"org": "org"}, ["repo-I1", "repo-I2", "repo-I3", "other-I4"], reader)
    assert len(rows) == 4
    assert not result.unavailable
    assert len(minted) == 2
    assert {args[2] for args in minted} == {"repo", "other"}


def test_named_reads_never_exceed_three_in_flight():
    lock = threading.Lock()
    active = peak = 0
    def get(path):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {"number": int(path.rsplit("/", 1)[-1]), "state": "closed"}
    rows = git_host._read_named(get, "org", "repo", list(range(9)), "test")
    assert len(rows) == 9
    assert peak == 3
