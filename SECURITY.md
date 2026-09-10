# Security Policy

## Reporting a vulnerability

If you find a security vulnerability in nousergon-console, please report it privately:

- **Preferred:** open a [GitHub Security Advisory](https://github.com/nousergon/nousergon-console/security/advisories/new). This keeps the discussion private until a fix ships.
- **Alternative:** email `security@nousergon.ai` with a description and reproduction steps.

Please **do not** open a public issue for security reports. We aim to acknowledge within 72 hours and ship a fix or mitigation within 14 days for high-severity issues.

## Scope

nousergon-console is a read-only, self-hosted fleet index: it never writes to any source it reads, and it persists nothing of its own. The sensitive surface is **credential handling and read access to the sources it is pointed at**. In scope:

- **Credential exposure:** any path that leaks an AWS credential, an SSM parameter, a git host token, or a SQL connection string configured in `config.yaml` — through logs, error messages, a rendered page, or the JSON representation of any view.
- **Injection:** SQL injection in the `sql-source` / `sql-query` adapters, path traversal in the object-store or YAML-registry adapters, or unsafe handling of a source-supplied value that reaches a shell or a filesystem path.
- **Authorization / scope escape:** any way a view, the search index, or `doctor` surfaces an entity, field, or source the deployer's `config.yaml` did not enable.
- **Supply-chain:** a dependency or install path that could execute untrusted code during `pip install` or `console index`.

Out of scope:

- DoS via traffic volume (self-host infrastructure; the deployer owns their own ingress).
- Cost-runaway from a deployer's own adapter configuration (e.g. an over-broad object-store prefix) — that is a configuration choice, not a defect in the console.
- Vulnerabilities in upstream dependencies not yet publicly disclosed — report those upstream first.
- Issues requiring local filesystem/process access on the host running the console (if that host is compromised, the threat model has already failed).

## Threat model assumptions

- **Single-operator or small-team self-host.** There is no multi-tenant model in this repository.
- **Credentials live in the deployer's own environment** — `config.yaml` (gitignored, never committed) references AWS credentials, SSM parameters, or connection strings that the console reads via `boto3` / the stdlib; it does not generate, store, or transmit credentials of its own.
- **All adapter/driver sources are read-only from the console's perspective.** The console never calls a write API against a configured source — an adapter or driver that does is itself the vulnerability.
- **Console-rendered content is treated as data, not code**, by any downstream consumer — the HTML and JSON views quote source-supplied values rather than execute them; a rendering path that fails to escape a source-supplied string is in scope above.

## Hardening recommendations for self-hosters

- Scope the IAM principal the console runs as to least privilege — read-only access to exactly the buckets, tables, and state machines named in your `config.yaml`.
- Keep `config.yaml` at `600` and never commit it; the tracked `config.example.yaml` is the shape, not a template to fill in and check in.
- Put the console's HTTP surface behind your own authenticator (this fleet uses Cloudflare Access) — the console itself does not implement authentication.
