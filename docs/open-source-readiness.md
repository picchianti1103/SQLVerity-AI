# Open-source readiness

SQLVerity AI is public as a `0.1.x` developer preview. This checklist separates repository hygiene,
artifact distribution, and production certification; completing one does not imply the next.

## Delivered in the release-preparation pass

- GitHub Actions runs a full-history secret scan, Ruff, strict mypy, pytest, a core dependency audit,
  a dependency-license inventory, the golden regression gate, wheel/sdist builds, distribution
  metadata validation, and a hardened-container smoke test.
- Dependabot tracks Python, GitHub Actions, and Docker base-image dependencies.
- Database drivers and the OpenAI SDK are optional packaging extras; the core web/API installation
  no longer pulls every enterprise connector.
- `.env.example` documents fail-closed provider selection and opaque local secret references.
- A non-root, read-only Docker runtime supports a pooled PostgreSQL application catalog. Compose
  provisions the synthetic demo in a separate database with a dedicated SELECT-only login, while
  the multi-stage image excludes source documentation, tests, and local verification artifacts.
- Contribution, pull-request, conduct, and private security-reporting expectations are documented.
- Apache-2.0 is declared through the complete license text and SPDX distribution metadata.
- Direct dependency licenses and notable vendor terms are recorded in `THIRD_PARTY_NOTICES.md`.
- The previous audited Docker image passed `/health`, `/ui`, authentication, non-root-user,
  read-only-root, and persistent-volume smoke checks on Docker Desktop's Linux engine. The updated
  PostgreSQL catalog/demo composition passes interpolation and is covered by hosted CI, but was not
  engine-tested in this local audit.
- Internal planning material is intentionally excluded from the publication tree and from the new,
  sanitized public Git history.
- The exact sanitized public root passed Gitleaks with one commit and no findings. The resolved core
  dependency graph has no known vulnerabilities, and the direct dependency-license inventory was
  reviewed locally.
- The `v0.1.0` developer-preview release is published from the sanitized public root after hosted CI
  passed.
- The MariaDB dialect and adapter remain implemented, but the packaging extra is withheld until the
  upstream Python distribution publishes a release that resolves `PYSEC-2026-217`.
- The canonical repository URL is `https://github.com/picchianti1103/SQLVerity-AI`.
- The README identifies the developer-preview status, core workflow, product boundaries, and known
  non-goals before the detailed delivery history.

## Completed for public repository launch

- Ownership and publication rights were reviewed for source code, fixtures, ADRs, and bundled assets.
- The public repository was created from the sanitized root; private vulnerability reporting and
  repository security features are enabled.
- Hosted Linux CI and the hardened container smoke test pass and are required on the default branch.
- The `v0.1.0` tag and developer-preview release point to the sanitized public history.

## Required before distributing release artifacts

- Configure the repository environments and PyPI/TestPyPI Trusted Publishers described in
  [the release process](releasing.md).
- Run the release workflows and review the generated provenance and SBOM attestations for the exact
  image, wheel, and source distribution.
- If an image is published for more than one architecture, run the smoke test on every advertised
  architecture, including Linux/arm64.

## Required before claiming production readiness

- Run the implemented disposable live integration fixtures for every database dialect claimed as
  supported and retain the reports for the exact release commit.
- Exercise at least one approved local model and every selected cloud provider end to end.
- Measure result-set execution accuracy, latency, context efficiency, and actual cost on a versioned
  benchmark rather than only validating structured proposals.
- Validate the delivered secret-manager, identity, quota, monitoring, retention, backup/restore, and
  disaster-recovery controls on the target platform. Add deployment-specific threat modeling,
  centralized log/metric/trace retention, on-call routing, break-glass policy, and measured RPO/RTO.

The authoritative functional gap list remains `docs/implementation-status.md`.
