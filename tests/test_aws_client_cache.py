"""console.aws memoises one client per (service, region)."""
import sys
import types
import pytest
from console import aws


@pytest.fixture(autouse=True)
def _clean():
    aws.reset()
    yield
    aws.reset()


def _fake_boto3(constructed):
    mod = types.ModuleType("boto3")

    def client(service, region_name=None):
        constructed.append((service, region_name))
        return object()

    mod.client = client
    return mod


def test_the_same_service_is_constructed_once(monkeypatch):
    """The whole point: the index rebuilds forever, and this was paid every pass."""
    constructed = []
    monkeypatch.setitem(sys.modules, "boto3", _fake_boto3(constructed))
    first = aws.client("s3")
    assert aws.client("s3") is first
    assert aws.client("s3") is first
    assert constructed == [("s3", None)]


def test_region_is_part_of_the_key(monkeypatch):
    """Collapsing region would silently send a caller to whichever region asked
    first — worse than the cost this removes."""
    constructed = []
    monkeypatch.setitem(sys.modules, "boto3", _fake_boto3(constructed))
    a = aws.client("stepfunctions", "us-east-1")
    b = aws.client("stepfunctions", "eu-west-1")
    assert a is not b
    assert aws.client("stepfunctions", "us-east-1") is a
    assert constructed == [("stepfunctions", "us-east-1"),
                           ("stepfunctions", "eu-west-1")]


def test_distinct_services_are_distinct_clients(monkeypatch):
    constructed = []
    monkeypatch.setitem(sys.modules, "boto3", _fake_boto3(constructed))
    assert aws.client("s3") is not aws.client("cloudwatch")
    assert len(constructed) == 2


def test_a_missing_boto3_still_raises_ImportError(monkeypatch):
    """Call sites keep their own try/except ImportError — that guard is how an
    adapter fails LOUD when the AWS extra is absent, instead of returning zero
    rows and painting a substrate ABSENT on a missing dependency."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def fake_import(name, *a, **kw):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *a, **kw)

    monkeypatch.delitem(sys.modules, "boto3", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(ImportError):
        aws.client("s3")


def test_reset_drops_the_cache(monkeypatch):
    constructed = []
    monkeypatch.setitem(sys.modules, "boto3", _fake_boto3(constructed))
    aws.client("s3")
    aws.reset()
    aws.client("s3")
    assert len(constructed) == 2
