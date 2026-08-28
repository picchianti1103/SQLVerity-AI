# SQLVerity AI

[![CI](https://github.com/picchianti1103/SQLVerity-AI/actions/workflows/ci.yml/badge.svg)](https://github.com/picchianti1103/SQLVerity-AI/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB.svg)](https://www.python.org/downloads/)

> **Developer preview — v0.1.0.** SQLVerity AI is not yet certified for unattended production use.

SQLVerity AI turns natural-language questions into SQL that people can inspect, validate, approve, and
run safely. It is designed for organizations that want the usefulness of AI-assisted querying
without giving a model uncontrolled access to their databases.

**New here?** Follow the [10-minute guided quickstart](docs/quickstart.md) with the bundled synthetic
PostgreSQL database. No production database or LLM credential is required to inspect the governed
catalog path.

The platform supports PostgreSQL, MySQL, MariaDB, Oracle, and SQL Server. LLM providers are
optional and disabled by default.

## Philosophy

SQLVerity AI is built around a few simple ideas:

- **The model proposes; the platform decides.** Generated SQL never runs directly.
- **Meaning must be inspectable.** Schema context, business mappings, assumptions, and SQL remain
  visible to the user.
- **Safety fails closed.** SQL must be read-only, valid for the selected schema, bounded, and
  approved before execution.
- **Share as little as possible.** Provider access is opt-in, classified content is governed before
  transfer, and result rows are processed locally.
- **Humans remain accountable.** Semantic corrections and query approval are explicit, versioned,
  and auditable.

The core flow is:

```text
question -> governed context -> SQL proposal -> validation -> EXPLAIN -> approval -> read-only result
```

## Who it is for

SQLVerity AI is for data teams that need natural-language access to relational data but cannot treat
generated SQL as trusted output. It is especially relevant when reviewers need to see what metadata
left the system, which SQL was approved, and why a query was allowed to run.

It is not a zero-configuration chatbot, a write-capable database agent, or a replacement for database
permissions and organizational review.

## First run with Docker

Docker Compose is the quickest way to try SQLVerity AI. It starts the application, its PostgreSQL catalog,
and a synthetic read-only demo database.

1. Copy the example environment file:

   ```powershell
   Copy-Item .env.example .env
   ```

   On macOS or Linux, use `cp .env.example .env`.

2. Open `.env` and replace the development bootstrap key and example passwords.

3. Build and start the services:

   ```powershell
   docker compose up --build
   ```

4. Open [http://127.0.0.1:8000/ui](http://127.0.0.1:8000/ui). Use the
   `SQLVERITY_BOOTSTRAP_API_KEY` value from `.env` for the initial administration flow.

To stop SQLVerity AI, press `Ctrl+C`, then run:

```powershell
docker compose down
```

The demo works without enabling an LLM: bundled schemas can be imported and explored locally.
Provider calls require an explicit provider selection, credentials, and an egress policy.

For the exact demo source values, validation checks, and sample questions, continue with the
[guided quickstart](docs/quickstart.md).

## Native development

SQLVerity AI requires Python 3.12 or newer. The native setup uses a local SQLite catalog by default.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,postgres]"
$env:SQLVERITY_BOOTSTRAP_API_KEY='replace-with-a-random-secret-of-at-least-32-characters'
```

SQLVerity AI can start without an LLM for local schema import and exploration. To generate SQL, choose a
provider before starting the API. For OpenAI:

```powershell
$env:SQLVERITY_LLM_PROVIDER='openai'
$env:OPENAI_API_KEY='your-api-key'
$env:SQLVERITY_OPENAI_MODEL='your-approved-model-id'
```

Or use a local Ollama model without a cloud API key:

```powershell
$env:SQLVERITY_LLM_PROVIDER='ollama'
$env:SQLVERITY_OLLAMA_MODEL='your-local-model'
```

Then start SQLVerity AI:

```powershell
uvicorn apps.api.main:app --reload
```

Open [http://127.0.0.1:8000/ui](http://127.0.0.1:8000/ui). Before the first model call, create an
explicit provider-egress policy in the console. See the
[configuration reference](docs/configuration.md#llm-providers) for other providers and settings.

## Current scope

SQLVerity AI already includes schema acquisition, governed semantic metadata, structured SQL generation,
dialect-aware AST validation, cost checks, explicit approval, bounded read-only execution,
deterministic result processing, privacy reporting, and provenance.

It does not yet claim complete live certification across every supported database and provider.
Cross-DataSource joins and a stateful multi-turn clarification dialogue are also outside the current
scope. See the [implementation status](docs/implementation-status.md) for the authoritative list of
delivered capabilities and known gaps.

## Documentation

- [Guided quickstart](docs/quickstart.md) — reach a verified demo catalog and, optionally, a first AI-assisted query.
- [Early-adopter guide](docs/early-adopter-guide.md) — a focused 30-minute evaluation and feedback format.
- [Roadmap](ROADMAP.md) — adoption milestones, near-term priorities, and explicit non-goals.
- [Configuration reference](docs/configuration.md) — providers, databases, secrets, and production
  settings.
- [Implementation status](docs/implementation-status.md) — delivered capabilities and known gaps.
- [Architecture decisions](docs/adr) — the reasoning behind the main technical boundaries.
- [Operations runbook](docs/operations-runbook.md) — health, observability, incidents, and recovery.
- [Migration and rollback](docs/migration-and-rollback.md) — safe catalog upgrades.
- [Release process](docs/releasing.md) — trusted Python and container publication.
- [Contributing](CONTRIBUTING.md) — development setup, tests, and design expectations.
- [Changelog](CHANGELOG.md) — release and delivery history.
- [Security policy](SECURITY.md) — private vulnerability reporting.
- [Maintainers](MAINTAINERS.md) — project ownership and release responsibility.

## Feedback

The most useful contribution at this stage is a real use case tested against synthetic or redacted
metadata. Use the [early-adopter feedback form](https://github.com/picchianti1103/SQLVerity-AI/issues/new?template=early-adopter.yml)
to report where setup, trust, or query review becomes unclear. Bugs and feature requests have separate
issue forms. Never include credentials, private schemas, prompts, result rows, or vulnerability details.

## License

SQLVerity AI is distributed under the [Apache License 2.0](LICENSE). Third-party components retain their
own terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
