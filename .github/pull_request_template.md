## What & why

<!-- What does this change and why? Link any related issue. -->

## Checklist

- [ ] Tests added/updated for the behavior change
- [ ] `pytest` passes locally — the coverage floor in `pyproject.toml` is a ratchet, raised as coverage improves and never lowered to make a change pass
- [ ] `ruff check --select F821 .` is clean
- [ ] `python -m console index --config config.example.yaml` and `python -m console check-namespace --config config.example.yaml` still pass, if this touches the index or a descriptor/registry shape
- [ ] `CHANGELOG.md` updated, if this repo tracks one for the change
- [ ] No topology literal (bucket, ARN, host, port, path, component id) added outside `config.example.yaml` — see `CONTRIBUTING.md`
- [ ] No real `config.yaml`, credential, or fleet-specific detail committed
- [ ] Fail-loud preserved — no new silent `except: pass` swallows

## Test plan

<!-- How you verified this works. -->

---

**Prepared by:** <!-- model name from the session prompt, e.g. claude-sonnet-5, deepseek-v4-flash, claude-haiku-4-5 — replaces the generic "Generated with Claude Code" footer -->
