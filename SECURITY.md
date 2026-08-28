# Security policy

SQLVerity AI executes generated SQL and handles database metadata, so security reports are taken seriously.
The project is currently a developer preview and has not completed production deployment hardening
or live integration certification.

## Supported versions

Security fixes are currently made only on the latest `0.1.x` revision on the default branch. There
are no long-term-support releases yet.

## Reporting a vulnerability

Do not open a public issue containing exploit details, credentials, database contents, prompts, or
other sensitive material. Use the repository's private vulnerability-reporting or Security Advisory
channel. If private reporting is not enabled, contact the repository owner privately through their
[GitHub profile](https://github.com/picchianti1103) before sharing technical details.

Include, when possible:

- affected revision and deployment topology;
- a minimal reproduction using synthetic data;
- expected and observed security boundaries;
- impact on SQL safety, authorization, prompt egress, secrets, privacy, or tenant isolation;
- suggested mitigations, if known.

The maintainers should acknowledge a complete report within five business days, coordinate a fix
and disclosure timeline, and credit the reporter unless anonymity is requested. Never test against
systems or data you do not own or have explicit permission to use.

## Deployment notice

Use least-privilege, read-only database accounts. The `env://` secret resolver and opaque API-key
authentication are development/minimum boundaries, not substitutes for a production secret manager,
identity federation, network isolation, rate limiting, monitoring, and an organization-specific
threat assessment. See `docs/implementation-status.md` for known gaps.

## Repository security checks

CI scans complete Git history for secrets and audits the core Python dependency graph against known
vulnerabilities. Release preparation also includes a direct-dependency license review documented in
`THIRD_PARTY_NOTICES.md`. These automated checks reduce risk but do not replace review of source,
generated artifacts, container layers, optional integrations, or deployment-specific dependencies.
