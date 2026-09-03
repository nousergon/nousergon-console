"""`infrastructure/deploy-on-merge.sh` step 2 — how the box gets its config.

`alpha-engine-config-I9802` moved the assembled console config off Parameter
Store: the SSM parameter now carries a POINTER (`config_source`,
`config_sha256`, `config_chars`, `generated_utc`) and an S3 object carries the
body. The Advanced tier caps a parameter value at 8,192 characters and the body
had reached 8,129 after ONE adapter fragment, so `console-policy` §2.6's
"onboarding a module is writing one file" was bounded by its transport.

These are behavioural tests, not `assert "..." in script.read_text()` tests. The
resolution block is executed with a stubbed `aws` on PATH, because the failure
this file exists to prevent — the box installing a config body that is not the
one the writer published — is invisible to any text assertion: the console comes
up HEALTHY on the wrong adapters, and nothing downstream can tell.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "infrastructure/deploy-on-merge.sh"

BODY = "console:\n  bind: 127.0.0.1\n  port: 5180\nadapters: []\n"


def _resolution_block() -> str:
    """The script from the top through step 2, with the box-specific prelude cut.

    Slicing at the literal step markers keeps this test bound to the real file:
    if step 2 is renamed or reordered the slice fails loudly rather than testing
    a copy that has drifted.
    """
    text = SCRIPT.read_text()
    start = text.index("# 2. private config")
    end = text.index("# 3. systemd unit")
    return text[start:end]


def _harness(tmp_path: Path, pointer: str, object_body: str | None,
             fetch_fails: bool = False) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    obj = tmp_path / "s3-object"
    if object_body is not None:
        obj.write_text(object_body)

    (bin_dir / "aws").write_text(textwrap.dedent(f"""\
        #!/bin/bash
        # Stub. `get-parameter` returns the pointer; `s3api get-object` copies the
        # fixture object to the destination path, the way the real CLI does.
        if [ "$1" = "ssm" ]; then
          cat {tmp_path / 'pointer'}
          exit 0
        fi
        if [ "$1" = "s3api" ]; then
          if [ "{'1' if fetch_fails else '0'}" = "1" ]; then exit 1; fi
          dest="${{@: -1}}"
          cp {obj} "$dest" || exit 1
          echo '{{}}'
          exit 0
        fi
        exit 1
        """))
    (bin_dir / "aws").chmod(0o755)
    # The box runs this as root; the test does not own ec2-user.
    (bin_dir / "chown").write_text("#!/bin/bash\nexit 0\n")
    (bin_dir / "chown").chmod(0o755)
    (tmp_path / "pointer").write_text(pointer)

    script = tmp_path / "resolve.sh"
    script.write_text(
        "#!/bin/bash\nset -uo pipefail\n"
        f'CONFIG_SSM="/alpha-engine/nousergon-console/config.yaml"\n'
        f'CONFIG_DST="{tmp_path / "config.yaml"}"\n'
        'log() { echo "$*"; }\n'
        'fail() { echo "FAIL $*"; exit 1; }\n'
        + _resolution_block()
    )
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)


def _pointer(body: str, digest: str | None = None, source: str | None = None) -> str:
    lines = [f"config_source: {source or 's3://alpha-engine-research/ops/console/config.yaml'}"]
    if digest is not False:
        lines.append(
            f"config_sha256: {digest or hashlib.sha256(body.encode()).hexdigest()}")
    lines += [f"config_chars: {len(body)}", "generated_utc: '2026-09-02T00:00:00+00:00'"]
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _needs_sha256sum():
    if shutil.which("sha256sum") is None:
        pytest.skip("sha256sum is not on PATH (the box is Amazon Linux; it is)")


class TestAPointerResolvesToTheBodyItNames:
    def test_the_body_is_installed(self, tmp_path):
        r = _harness(tmp_path, _pointer(BODY), BODY)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (tmp_path / "config.yaml").read_text() == BODY

    def test_the_installed_file_is_0600(self, tmp_path):
        _harness(tmp_path, _pointer(BODY), BODY)
        assert oct((tmp_path / "config.yaml").stat().st_mode)[-3:] == "600"

    def test_no_config_value_reaches_stdout(self, tmp_path):
        """Config values are fleet topology. The deploy log carries byte counts
        and a source URI, never a body."""
        r = _harness(tmp_path, _pointer(BODY), BODY)
        assert "127.0.0.1" not in r.stdout and "127.0.0.1" not in r.stderr


class TestADigestMismatchInstallsNothing:
    """The failure mode the digest exists for: the pointer and the body drift
    apart, and the console comes up HEALTHY on a config nobody published."""

    def test_it_fails(self, tmp_path):
        r = _harness(tmp_path, _pointer(BODY), "console: {}\n")
        assert r.returncode != 0
        assert "drifted apart" in r.stdout + r.stderr

    def test_it_leaves_no_partial_config_behind(self, tmp_path):
        """`aws s3api get-object` writes the destination before anything can
        check it, so refusing without removing it installs the bad body."""
        _harness(tmp_path, _pointer(BODY), "console: {}\n")
        assert not (tmp_path / "config.yaml").exists()

    def test_a_pointer_with_no_digest_is_refused(self, tmp_path):
        r = _harness(tmp_path, _pointer(BODY, digest=False), BODY)
        assert r.returncode != 0
        assert "config_sha256" in r.stdout + r.stderr

    def test_an_unreadable_body_fails_rather_than_falling_back(self, tmp_path):
        """Falling back to the pointer text as a config would hand the console a
        four-key document it would happily build an empty surface from."""
        r = _harness(tmp_path, _pointer(BODY), BODY, fetch_fails=True)
        assert r.returncode != 0
        assert "could not read the config body" in r.stdout + r.stderr

    def test_an_unparseable_source_is_refused(self, tmp_path):
        r = _harness(tmp_path, _pointer(BODY, source="alpha-engine-research"), BODY)
        assert r.returncode != 0
        assert "unparseable config_source" in r.stdout + r.stderr


class TestAWholeBodyInTheParameterStillWorks:
    """The two ends of this migration are in different repos — this script and
    `nous-ergon-ops/scripts/console_config.py` — and neither may assume the
    other has landed."""

    def test_a_parameter_with_no_config_source_is_the_config(self, tmp_path):
        r = _harness(tmp_path, BODY, None)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (tmp_path / "config.yaml").read_text().rstrip("\n") == BODY.rstrip("\n")

    def test_it_is_still_0600(self, tmp_path):
        _harness(tmp_path, BODY, None)
        assert oct((tmp_path / "config.yaml").stat().st_mode)[-3:] == "600"
