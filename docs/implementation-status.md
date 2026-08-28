# Implementation status and known gaps

- **Last updated:** 2026-08-25
- **Tracking rule:** `Blocked` means the current environment lacks a required external capability.
  `Deferred` means the work is feasible but belongs to a later document-aligned increment.

## Delivered

| Area | Current state |
|---|---|
| English-first product guidance | English is the canonical console locale through a dependency-free message catalog; the System workspace includes a state-aware seven-step onboarding checklist, every primary workspace has contextual help, and a searchable responsive Help drawer exposes versioned task guidance and a glossary without persisting tokens, questions, parameters, or results |
| Catalog foundation | Tenant-scoped, versioned schema catalog with local SQLite and operational pooled PostgreSQL repositories, packaged migrations, advisory-locked upgrades, readiness checks, and backend selection at startup |
| Schema acquisition | PostgreSQL, MySQL, MariaDB, Oracle, and SQL Server metadata connectors plus dialect-aware DDL and manual schema import |
| Semantic governance | Immutable evidence, review queue, correction history, and optimistic concurrency |
| LLM boundary | Provider-neutral gateway, structured output contract, prompt egress policy, and usage events |
| Privacy-first AI egress | Dedicated pre-query Privacy and AI step; provider/model/deployment disclosure; tenant/DataSource precedence; acknowledgement bound to deployment claims; local provider-free SQL preflight using the exact generation manifest; server classification reason-code propagation; short-lived HMAC, actor/context/policy-bound single-use confirmation; structured content-free block errors; and append-only transfer receipts with counts, latency, token/cost linkage, and no prompt, matched value, parameter, row, credential, or provider response content |
| Semantic inference | Strict `INFERRED` proposals integrated with the review queue |
| Context retrieval | Lexical ranking, confirmed-description matching, FK graph expansion, and context preview |
| SQL generation | Structured `SQLProposal` limited to non-redacted retrieved context and checked against its own AST references; privacy-selectable semantic retry remains bounded to the same governed context and never bypasses validation |
| Intent interpretation | Every proposal includes a governed intent kind, summary, requested row limit, explained table/column mappings, calibrated confidence, and in-context alternatives; explicit Italian/English preview limits are checked against both interpretation and SQL |
| Intent correction memory | Data stewards can correct mappings through catalog choices or a free-text follow-up in Query Studio; conversational corrections use governed structured LLM output limited to current-catalog, same-role candidates, require at least 0.75 confidence and no unresolved ambiguity, and otherwise modify no memory. Accepted corrections create or supersede DataSource-scoped confirmed Business Concepts with immutable history, classification propagation, and pending-ticket invalidation when SQL must be regenerated |
| SQL safety | Registry-routed PostgreSQL/MySQL/MariaDB/Oracle/SQL Server AST parsing, single-query SELECT-only enforcement, read-only CTE/set operations, catalog reference checks, per-dialect function policy, wildcard policy, and dialect-native bounded previews |
| Governed generated-query parameters | Structured proposals declare up to 50 named scalar parameters with string/integer/number/boolean/date/datetime/UUID types; AST validation requires exact placeholder/declaration agreement and static preview limits; typed values are driver-bound and SHA-256-bound across EXPLAIN/approval/execution without persisting or auditing raw values |
| Query lifecycle | Tenant-scoped persisted query tickets, catalog-version binding, explicit approval, execution/failure/completion transitions, and content-minimizing audit events |
| PostgreSQL execution | Non-`ANALYZE` JSON `EXPLAIN`, transaction read-only mode, server-side statement timeout, row and serialized-byte bounds, result metadata, and active cancellation |
| MySQL/MariaDB execution | Official vendor-driver integrations, non-`ANALYZE` JSON `EXPLAIN`, read-only transactions, vendor-specific statement timeouts, bounded streaming fetches, and separate-connection `KILL QUERY` cancellation. The MariaDB packaging extra is temporarily withheld pending an upstream security fix. |
| Oracle execution | Official `python-oracledb` Thin driver, TCPS-by-default secrets, `USER_*` introspection, read-only transactions, per-round-trip call timeout, bounded fetches, `Connection.cancel()`, and rollback-scoped `EXPLAIN PLAN`/`DBMS_XPLAN` output |
| SQL Server execution | Official `mssql-python` driver, encrypted keyword-argument connections, driver read-only access mode, connection-level query timeout, governed named-to-positional parameter binding, bounded fetches, non-executing SHOWPLAN XML, and validated-SPID separate-connection cancellation |
| Result processing | Local deterministic summaries for empty/scalar/row/table results, bounded scalar formatting, and no automatic LLM interpretation |
| Result privacy | Catalog-version classification reload before execution, fail-closed gaps, per-output-column lineage through aliases/expressions/CTEs with selective masking, conservative whole-result fallback for unresolved shapes, and an explicit metadata-only privacy report |
| Result provenance | SQL, DataSource, catalog version, lineage, concepts, assumptions, approval, planner estimates, execution metadata, and linked LLM usage/cost identifiers |
| FinOps pricing | Tenant-scoped, effective-dated model pricing with exact decimal arithmetic, cached-token rates, batch discounts, source versions, overlap rejection, and PostgreSQL exclusion constraints |
| FinOps usage and budgets | Deterministic pre-call and actual cost calculation, currency/pricing provenance, monthly tenant summaries, and fail-closed pre-call budget enforcement |
| Database cost governance | Per-DataSource `EXPLAIN` requirements plus planner-cost and estimated-row thresholds enforced before approval |
| Authorized Query DataSource | Immutable versioned base-query definitions exposed as one virtual catalog object, declared output schema and parameters, filtering/aggregation policy, AST wrapping, bound driver parameters, and EXPLAIN/approval binding integrity |
| Golden evaluation | Versioned 50-question dataset across three realistic domains, reference and external-prediction runner, PostgreSQL AST/lineage/semantic/safety checks, strict thresholds, hash-bound baseline, per-case regression detection, and non-zero CLI gate |
| Corrected-SQL learning loop | Immutable human correction evidence linked to question, DataSource, catalog version, optional QueryRequest, actor and reason; AST revalidation; explicit revision chains; deterministic similarity retrieval; schema-drift filtering; Context Builder seeding; and classification-aware prompt egress |
| Business concepts and synonyms | DataSource-scoped immutable definitions and mutable resolutions; epistemic precedence and explicit conflicts; review/history APIs; optimistic human correction; Unicode-normalized governed term resolution; physical-object Context Builder seeding; classification-aware egress; and provenance limited to non-redacted concept keys |
| Metric Definitions and Business Rules | Immutable DataSource-scoped evidence/resolutions; formula, grain, dimension, concept and rule dependencies; dialect-aware AST fragment validation; review/history/correction APIs; physical context seeding; transitive classification; prompt redaction atomicity; exact formula/predicate verification in generated SQL; and metric/rule result provenance |
| Feedback and golden curation | One immutable final accepted/rejected/corrected outcome per eligible QueryRequest; active correction/source binding; real DataSource acceptance/correction rates; current-schema eligibility; immutable golden candidate snapshots and final human reviews; approved-only deterministic export; tenant isolation; and content-minimizing audit events |
| Authentication, federation, and RBAC | Fail-closed Bearer authentication for `/v1`; environment-only bootstrap authority; tenant-scoped principals; tenant/DataSource role assignments; one-time opaque API keys with hash-only persistence, expiry and append-only revocation; pre-provisioned OIDC subject binding; issuer/audience/JWKS verification; optional MFA/ACR enforcement; browser Authorization Code + PKCE with state/nonce, HttpOnly session cookies and CSRF protection; server-derived actors; actor-spoof rejection; and OpenAPI security declaration |
| Repository audit and optimization | Full static/architectural review; tenant/version-scoped bulk catalog reads replacing object/column N+1 access; fixed-query security and golden-candidate listings; shared Unicode term normalization; shared catalog configuration and resource-closing helpers; crash-bounded quota leases; atomic background-job continuation; isolated least-privilege demo storage; stricter load-gate semantics; generated-artifact cleanup; and documented residual risks. These are maintenance passes, not additional functional increments. |
| Open-source distribution groundwork | Apache-2.0 licensing and SPDX metadata; public CI for secret scanning, dependency auditing, license inventory, lint, strict typing, tests, golden regression, and distribution builds; optional provider/database extras; multi-stage non-root container and Compose quickstart; example environment; Dependabot; contribution, conduct, pull-request, private security-reporting, third-party-notice, and public-release guidance. Hosted CI execution and publication-rights review remain release gates, so this is not an additional functional increment. |
| OpenAI cloud provider | Explicit opt-in environment configuration; official Python SDK and Responses API; strict JSON-schema output; stateless `store=false` calls; fixed official endpoint; defensive completion/JSON/usage validation; cached-token and latency telemetry; conservative pre-call budget estimate; simulated provider/API-startup tests; and no credential persistence or logging |
| Multi-cloud LLM providers | Anthropic Claude Messages structured output, Google Gemini GenerateContent JSON Schema, and Kimi/Moonshot Chat Completions JSON Schema; fixed official HTTPS endpoints; deterministic requests; conservative estimates; usage/cached-token telemetry; explicit simultaneous selection with aliases; fail-closed protocol handling; dynamic console discovery; and no credential persistence or startup network call |
| Ollama local/private provider | Explicit opt-in configuration; native JSON-schema structured output; deterministic temperature; completion/JSON/usage validation; conservative budget estimate; loopback-only default; HTTPS plus explicit opt-in for remote private endpoints; optional in-memory bearer token; and no network health check |
| Dialect expansion | Central capabilities/alias registry with fail-closed routing; MySQL, MariaDB, Oracle, and SQL Server validation, semantic fragments, DDL/manual import, introspection, execution, EXPLAIN, timeout, cancellation, API wiring, and official drivers |
| Self-service console | Same-origin responsive English-first Control Plane, Query Studio, and administration view; state-aware Getting Started checklist, contextual explanations and searchable Help drawer; in-memory Bearer or OIDC browser session; tenant/DataSource discovery and creation; federated-principal provisioning; provider policy, connection-test, and background-job controls; opaque secret references; introspection, DDL and manual import; versioned Schema Explorer; visible intent interpretation, ambiguity candidates, conversational correction and explicit catalog override for authorized stewards; typed parameter editor with non-persisted values; governed proposal with maximum-privacy or semantic-retry choice, validation, EXPLAIN, approval, read-only execution, deterministic results, privacy and provenance; strict CSP and no external frontend runtime |
| Production security controls | Vault KV v2 and AWS Secrets Manager resolvers with fresh resolution for rotation, explicit backend allowlisting, secret-safe failures, an audited non-persisting connection test, deterministic server-side text classification that can only elevate client labels, and fail-closed provider egress policy per tenant/DataSource for purpose, classification, residency, and retention |
| Capacity and provider resilience | PostgreSQL-coordinated request windows and concurrent leases per user, tenant, and DataSource across replicas; crash-orphaned concurrency resets at the next request window while stale releases cannot affect the new window; `429`/`Retry-After`; bounded transient retry and circuit breaking for REST providers; bounded official-SDK retry configuration for OpenAI; and lease-based durable background jobs with crash recovery, cancellation, bounded retry, and transactionally enqueued cursor-batched semantic inference continuations |
| Operational readiness | Request IDs; bounded-cardinality Prometheus counters, histograms, and worker gauges; structured content-free request logs; opt-in W3C/OTLP tracing; catalog/worker readiness; Prometheus alert rules and runbook; authorized audit listing and NDJSON export; checksummed backup/restore; isolated restore drills; and confirmation-gated operational retention with append-only run evidence |
| Live certification harness | Synthetic PostgreSQL fixture spanning the golden domains; opt-in real catalog/introspection/EXPLAIN/read-only execution tests; opt-in minimal real provider calls; reference-result execution accuracy with latency percentiles and failure accounting; bounded HTTP load profiling; manual live certification; and scheduled PostgreSQL restore-drill workflows |

## Roadmap accounting

- **Delivered:** 26 technical increments. Phase 8 now includes corrected-SQL evidence, governed
  Business Concepts/Synonyms, Metric Definitions, Business Rules, final user feedback, and reviewed
  promotion into golden evaluation candidates after completion of the original concrete sequence.
- **Concrete implementation sequence:** 20 of 20 entries are complete. The provider-neutral gateway
  now has a concrete cloud adapter, and the final automation entry is represented by the delivered
  Phase 8 learning-loop increments.
- **Document MVP scope:** 19 of 19 explicitly included capabilities are delivered. OpenAI provides
  the concrete cloud path and Ollama provides the concrete local/private path.
- **Full document roadmap:** 3 macro-phases remain incomplete, but all are partially implemented.
  Phase 8 has the governed learning core; Phase 9 has Authorized Query DataSources and multiple
  direct database dialects but lacks hybrid retrieval; Phase 10 has API-key/OIDC authentication,
  browser federation, MFA enforcement, RBAC, secret-manager integrations, quotas, and operational
  controls, while deployment-specific hardening and retained production evidence remain external
  release work. Each macro-phase can require
  multiple technical increments and is not counted as a single sprint-sized change.

## Latest offline verification

- Pytest: 292 tests, 3 opt-in live tests skipped, and 50 subtests passed.
- Ruff: all configured checks passed.
- Strict mypy: 165 source files passed.
- Golden gate: 50 of 50 cases passed with zero regressions.
- Dependency integrity, sdist/wheel build with installed build requirements, and Twine metadata
  validation: passed. Hosted CI repeats the build in an isolated environment.
- Docker Compose interpolation with separate catalog/demo credentials: passed on this revision.
  Image build, container health, `/health`, `/ui`, authentication, non-root user, read-only root
  filesystem, and persistent-volume smoke checks passed on the previous audited baseline; the
  changed image/Compose path awaits the hosted CI container job because this run had no Docker daemon.
- Gitleaks: no secrets found across six commits. Isolated core dependency audit: no known
  vulnerabilities found. Direct runtime and optional dependency licenses were reviewed and recorded
  in `THIRD_PARTY_NOTICES.md`.
- Official MySQL Connector/Python 9.7.0 and MariaDB Connector/Python 1.1.14 connection contracts are
  covered offline through injected connections; no live server was used. The MariaDB release is
  affected by `PYSEC-2026-217`, so its distribution extra is withheld until a fixed release exists.
- Official OpenAI SDK 2.54.0 request/response contract: passed through a local mock transport; no
  provider credential, external request, or billable usage was involved.
- Anthropic Claude, Google Gemini, and Kimi/Moonshot REST contracts: passed through injected local
  HTTP clients; no provider credential, external request, or billable usage was involved.
- Official python-oracledb 4.0.2 and mssql-python 1.13.0 are installed. Their public connection,
  timeout, read-only, and cancellation-related contracts were inspected locally; database behavior
  is covered with injected connections because no live Oracle or SQL Server instance was used.

## Blocked in the current environment

| Item | Why it was not implemented | Unblock condition |
|---|---|---|
| Live PostgreSQL execution evidence | A PostgreSQL 17 fixture, real integration tests, and manual CI service job are implemented, but this local run had no reachable Docker daemon or approved external DSN, so no live result is claimed. | Run the manual live-certification workflow or provide a disposable PostgreSQL secret reference. |
| Live MySQL/MariaDB integration fixtures | No reachable disposable MySQL or MariaDB service is available. DDL, driver configuration, introspection, plan parsing, timeout selection, result bounds, and cancellation are covered through AST and injected-connection tests only. | Provide non-production MySQL and MariaDB DSNs/secret references or CI services with read-only test users. |
| Live Oracle/SQL Server integration fixtures | No reachable disposable Oracle or SQL Server service is available. Catalog permissions, TLS chains, `PLAN_TABLE`/`DBMS_XPLAN`, SHOWPLAN, result types, cancellation permissions, and same-instance routing are covered only at the adapter/protocol level. | Provide non-production Oracle and SQL Server connection references or CI services with least-privilege test users. |
| Live Ollama model fixture | The adapter and official HTTP contract are tested with an injected client, but no approved local model/runtime is running in this workspace. | Start an approved Ollama model and provide its model id; for a remote private endpoint, also provide HTTPS and any required bearer credential. |
| External-provider end-to-end evidence | Opt-in real calls exist for OpenAI, Anthropic Claude, Google Gemini, Kimi/Moonshot, and Ollama, but no approved non-production credentials/models were available for this run. | Configure the protected live-certification environment and explicitly dispatch the selected provider rows. |
| Live golden execution and runtime KPIs | The runner and expected-result fixture now compute execution accuracy and p50/p95 candidate latency, but no real prediction artifact/database run was available. Average provider cost and context efficiency remain outside this runner. | Run `sqlverity-live-certify` with one complete, hash-bound prediction artifact per certified model/prompt configuration. |
| Cross-model golden comparison | The runner accepts external prediction files and OpenAI can now generate them, but no approved credentials or prediction artifacts are configured. | Produce one complete prediction artifact per reviewed model/prompt configuration. |
| Safe MariaDB Python distribution extra | The latest public `mariadb` package available during the release audit is affected by `PYSEC-2026-217`; no corrected PyPI release is available. The dialect and adapter remain implemented, but the extra is withheld from `pyproject.toml`. | Review an upstream fixed release and its Connector/C/native-library combination, then restore the extra and audit it in CI. |
| Hosted release controls | Local Git can publish the reviewed source, but branch protection, required-check policy, maintainer assignment, tagged artifacts, SBOM/signature publication, and the first hosted workflow evidence are repository-owner release actions. | Configure the GitHub repository settings and run a reviewed release workflow on the exact tagged commit. |

## Deferred by roadmap

| Item | Planned increment / reason |
|---|---|
| Secret lifecycle administration | Environment, Vault KV v2, and AWS Secrets Manager references plus audited connection testing are delivered. Secret creation, revocation, rotation scheduling, KMS policy management, and privileged UI remain deployment/control-plane responsibilities. |
| Query history and saved workspaces | Query Studio now supplies typed, non-persisted parameter bindings through the full lifecycle. Drafts, searchable history, and shareable workspaces remain deferred; API keys and parameter values are intentionally never persisted by the browser. |
| Configurable safe UDF allowlists | Unknown anonymous functions currently fail closed; approved organization-specific read-only functions need tenant/data-source policy configuration. |
| Advanced admission scheduling | Shared quotas, durable jobs, semantic batching, and a load-test gate are delivered. Weighted tenant fairness and a separately packaged worker deployment remain deferred. |
| Hard database-wire byte cap | Results are fetched in batches and serialized output is byte-bounded; one oversized database value can still be allocated by the driver before the application rejects it. |
| Governed metric/aggregation answer templates | The metric catalog is delivered, while reusable answer-format templates and their policy contract remain unimplemented. |
| Optional narrative result interpretation | No result rows are sent to an LLM. A future opt-in interpreter requires explicit result-egress policy, reduction, and local/private provider support. |
| Embeddings, vector storage, hybrid reranking | Lexical plus graph retrieval is the deterministic baseline; no vector backend has been selected. |
| Automated concept inference and synonym promotion | Cursor-batched schema-description inference is supported, but there is no governed workflow that extracts concepts from descriptions/corrections, and corrections are never promoted automatically. |
| Advanced metric semantics | Derived metric-to-metric DAGs, semantic calendars, units/currency conversion, formatting contracts, non-row rules, and cross-DataSource metrics are not implemented. |
| Non-PostgreSQL Authorized Query surfaces | Direct DB, DDL/manual/hybrid catalog flows support MySQL, MariaDB, Oracle, and SQL Server, but the specialized parameterized Authorized Query compiler remains PostgreSQL-only pending dialect-specific placeholder and wrapper contracts. |
| Full semantic authoring UI and multi-turn clarification | APIs expose concept, metric and rule list/proposal/review/history/correction flows, and Query Studio now supports both catalog-choice and free-text intent correction. Dedicated concept/metric/rule editors and a stateful multi-turn clarification dialogue remain separate increments. |
| Full pre-validation QueryRequest lifecycle | Validated/rejected tickets and later transitions are persisted; `RECEIVED` through LLM/validation failure stages are still ephemeral. |
| Advanced identity administration | OIDC bearer/browser federation, pre-provisioned subject binding, MFA/ACR enforcement, CSRF protection, and federated-principal UI are delivered. Recovery UX remains identity-provider-owned; SAML, group synchronization, service-account rotation, role removal/change workflows, and break-glass policy remain separate capabilities. |
| Deployment observability and regional DR | OTLP export, Prometheus metrics/alerts, content-free logs, retention tooling, and isolated scheduled restore drills are delivered. Centralized storage, on-call integration, cross-region targets, and measured production RPO/RTO require the deployment platform and retained drill evidence. |
| FinOps simulator and provider comparison UI | The deterministic cost engine and summary API are delivered; scenario comparison, recommendations, and a frontend are separate product increments. |
| Project/user budgets and currency conversion | This increment enforces monthly tenant budgets in the pricing currency. Project/user attribution, exchange-rate sources, alerts, and budget carry-over are not yet modeled. |
| Pricing lifecycle automation | Prices are configured through immutable effective-dated entries; automatic provider-price ingestion and scheduled interval closure are not implemented. |
| Object-level classification | Columns are classified; schema-object descriptions currently use the conservative `INTERNAL` prompt classification. |
| Live Authorized Query schema verification | The declared virtual output schema is validated against the base-query projection names. The new live PostgreSQL harness does not yet compare every declared physical result type across complex expressions. |
| Complex Authorized Query parameters | Binding is intentionally limited to JSON scalar values. Arrays, composite values, ranges, and file/blob parameters require type-specific adapters and policy. |
| Parameter-signature hardening | Raw values are never persisted or audited; a SHA-256 signature binds lifecycle calls. A deployment-secret HMAC or short-lived encrypted binding store is deferred until key management exists. |
| Multi-surface Authorized Query composition | One Authorized Query DataSource exposes one virtual object per version and the outer query may reference it once. Governed joins across several virtual surfaces need an explicit cross-surface policy. |
| Golden answer-level execution evidence | The live runner compares real candidate and reference result values for every expected-accepted case and the fixture is seeded. A certified prediction artifact and dispatched live run are still required before publishing an accuracy number. |
| Golden latency, cost, and context-efficiency gates | These fields are explicitly reported as unmeasured. They require live provider telemetry and runtime measurements. Acceptance and correction feedback are now measured separately per DataSource. |
| Golden fixture promotion automation | Approved candidates have a deterministic export and the committed domains now have a live schema/result fixture. Automatic merge, version bumps, duplicate/coverage adjudication, and reviewed expected-result changes still require an explicit release workflow. |

## Implemented initiative: privacy-first AI egress onboarding and explainability

### Priority and product intent

- **Status:** delivered as technical increment 25. Deployment-specific privacy/legal review remains
  an external release gate before presenting the console as self-service for non-specialist users.
- **Security posture:** preserve every current fail-closed server-side boundary. This increment must
  explain and stage authorization; it must not weaken classification, redaction, provider policy,
  RBAC, FinOps, or validation.
- **Primary outcome:** before any external-provider call, a user can understand which operation will
  call which provider and model, what categories of content may leave SQLVerity AI, which policy permits
  that transfer, what remains local, and whether the attempted call actually occurred.
- **Secondary outcome:** provider policy becomes an explicit prerequisite in the normal setup
  journey instead of a mandatory configuration hidden inside a later administration screen.

### Delivered architecture

- `POST .../sql/preflights` builds the production retrieval context and structured request, evaluates
  the effective policy, classifies the question, returns safe manifest counts/identifiers, records a
  non-invoked receipt, and never calls the provider.
- A short-lived HMAC confirmation is bound to actor, tenant, DataSource, provider/model, purpose,
  catalog version, effective policy id/update timestamp, question digest, privacy mode, and full
  content-manifest digest. It is single-use and rejects question, context, catalog, policy, provider,
  model, actor, or privacy-mode drift.
- Policy acknowledgements are bound to provider/model, deployment type, purpose set, classification
  ceiling, scope, residency, and retention. Missing or mismatched acknowledgements appear as
  `review_required` without silently broadening policy.
- Provider-policy failures expose stable safe codes and `provider_invoked=false`; sensitive matches,
  prompt text, schema descriptions, credentials, raw parameter values, database rows, and provider
  response content are excluded from errors, logs, receipts, and audit details.
- Append-only transfer receipts contain classifications, safe detector codes, per-kind counts,
  decision/confirmation outcome, invocation status, manifest digest, policy linkage, and successful
  token/latency/cost linkage. They do not reconstruct or retain the prompt.
- The console orders Privacy and AI before Query Studio, distinguishes external from local/private
  deployments, explains tenant/DataSource precedence and ambiguous residency/retention values,
  blocks missing/denied/review-required setup, performs the local disclosure, and requires an
  explicit confirm-and-send action.

### Original usability and privacy gaps (resolved by increment 25)

The following list records the gap analysis that drove the implementation; these items are no
longer descriptions of the current console.

1. The navigation places Query Studio before Administration even though an explicit provider policy
   is required by default. A user can therefore complete tenant, DataSource, and schema setup, reach
   generation, and discover the missing prerequisite only through a failed request.
2. The label `Provider policy` does not explain that the control authorizes data egress to an AI
   provider. `Maximum classification` can be confused with the classification assigned to the
   question, even though one is a data label and the other is an authorization ceiling.
3. `unspecified` residency and `provider_default` retention are technically accurate configuration
   values but are not translated into their privacy consequences. Neither value constitutes an EU
   residency or zero-retention guarantee.
4. Query Studio does not provide a preflight summary of the effective content manifest. Users do
   not see the selected schema-context size, classification distribution, policy scope, provider,
   model, purpose, residency, retention, or possible second call before pressing Generate.
5. The deterministic server-side classifier can elevate a client-declared label, but its safe reason
   codes are discarded at the HTTP boundary. The generic `Policy redacted required prompt content`
   response does not say which required content kind was redacted, which levels conflicted, what
   policy won, or that provider invocation was prevented.
6. Tenant and DataSource policies are displayed, but precedence is not explained where the user
   makes the decision. A narrower DataSource policy silently wins over the tenant baseline.
7. The UI states that proposals are not executed automatically, but this is different from whether
   metadata and a question are transmitted to an external AI. Those two trust boundaries need
   separate, persistent explanations.
8. The maximum-privacy and governed-semantic modes do not state prominently that the latter may
   perform a second provider call using the same already-filtered governed context.

### Information architecture and required setup order

The primary navigation should become:

1. **System** — authenticate, select/create the tenant, and discover configured providers.
2. **Sources** — register and test a DataSource using an opaque secret reference.
3. **Schema and classification** — introspect/import locally, inspect the catalog, and review the
   classifications that may govern later egress.
4. **Privacy and AI sharing** — review provider deployment claims, define the tenant baseline and
   optional DataSource override, preview allowed operations, and provide informed authorization.
5. **Query Studio** — prepare context, preview the effective transfer, confirm, generate, validate,
   EXPLAIN, approve, and execute.
6. **Administration** — identities, role management, advanced policy maintenance, durable jobs,
   operational controls, and audit access.

The policy editor may remain reachable from Administration for maintenance, but the first valid
policy must be created or explicitly reviewed in the dedicated Privacy and AI sharing step. Query
Studio must show a blocking setup card with a direct link to that step when no effective policy
exists; it must not rely on a provider-call failure as onboarding.

Tenant policy is the baseline. A DataSource override can be created only after selecting a source,
must be visibly labeled as the effective narrower rule, and must show that it takes precedence over
the tenant policy for that provider and source. The UI must never silently broaden an existing
policy during onboarding or migration.

### Operation-by-operation disclosure contract

The dedicated privacy surface and Query Studio help must expose this matrix in user-facing language:

| Operation | AI provider call | Content that may be sent |
|---|---:|---|
| Database connection test | No | Nothing |
| Schema introspection or offline DDL/manual import | No | Nothing |
| Schema Explorer and local classification review | No | Nothing |
| SQL proposal generation | Yes | User question, target dialect/generation constraints, and the policy-filtered retrieved schema/semantic context described below |
| Governed semantic retry | Yes, possible second call | The same already-filtered governed context plus the bounded semantic retry instructions; no newly bypassed content |
| Schema-description inference | Yes | Selected schema metadata for the batch allowed by the `semantic_description_inference` purpose |
| Free-text intent-correction interpretation | Yes | Correction text, current interpreted entities, and same-role catalog candidates allowed by the `intent_correction_interpretation` purpose |
| Catalog-choice correction without LLM interpretation | No | Nothing |
| SQL validation | No | Nothing |
| Database `EXPLAIN` | No | Nothing to an AI provider |
| Human approval | No | Nothing to an AI provider |
| Read-only execution and cancellation | No | Nothing to an AI provider |
| Deterministic result processing, masking, and provenance | No | Database rows and rendered results remain local |

For SQL proposal generation, the preflight must distinguish content that is always required from
content that is conditional on retrieval and policy:

- **Required:** the complete question text as entered by the user; its declared, detected, and
  effective classifications; target SQL dialect; catalog-version binding; deterministic intent and
  requested-row-limit hints.
- **Retrieved schema context:** selected schema/table/view references; object kind; selected column
  references; physical types; nullability; primary-key indicators; confirmed descriptions; and
  selected relationship endpoints/keys.
- **Conditional governed context:** confirmed Business Concepts and synonyms; Metric Definitions;
  mandatory Business Rules; compatible corrected-SQL examples; and confirmed intent memories that
  were selected for the current question.
- **Provider request control:** trusted SQLVerity AI instructions and the strict structured-output schema.
  These are transmitted but contain no database rows or credentials.

The UI must state prominently that any literal placed directly in the natural-language question is
part of the question sent to the provider unless policy blocks the call. Users should be encouraged
to avoid real identifiers and secrets and to use governed typed parameters where applicable.

The following must be listed explicitly as **not sent to an AI provider** by the current product:

- database usernames, passwords, DSNs, secret payloads, and opaque secret references;
- bootstrap credentials, personal Bearer credentials, OIDC tokens, and provider API keys;
- database result rows, raw parameter bindings, serialized result values, and masked originals;
- `EXPLAIN` plans, approval credentials, cancellation handles, and active connection identifiers;
- local catalog backup content and operational audit exports.

SQLVerity AI's local retention statement must be separate from the provider statement: SQLVerity AI does not
persist prompt content in its catalog, while provider-side processing and retention follow the
configured account/deployment terms. `provider_default` must be rendered as “provider/account
default; not a zero-retention assertion,” and `unspecified` as “no residency guarantee has been
recorded in SQLVerity AI.” The UI must not infer contractual guarantees from an API key or model id.

### Dedicated Privacy and AI sharing experience

The new step must provide:

1. A plain-language explanation of the egress boundary, separate from SQL execution safety.
2. Provider cards showing provider id, exact model id, enabled purposes, deployment residency claim,
   retention claim, and whether the provider is local/private or an external cloud service.
3. A clear distinction between:
   - **content classification:** what the data is;
   - **maximum allowed classification:** what the organization authorizes for this provider;
   - **effective classification:** the stricter of the user declaration and server detection.
4. Classification examples and consequences. Detection reason codes may be explained without
   reproducing the matched sensitive value.
5. Separate purpose toggles with descriptions for `sql_proposal_generation`,
   `semantic_description_inference`, and `intent_correction_interpretation`; no implicit “all future
   purposes” choice.
6. An explicit scope selector for tenant baseline versus selected-DataSource override, accompanied by
   an effective-policy preview and precedence explanation.
7. A required acknowledgement summarizing the selected provider, model, purposes, classification
   ceiling, residency claim, and retention claim before saving an allowing policy.
8. A conspicuous denial path. Administrators must be able to keep a provider configured but deny its
   use for a tenant or source without deleting credentials or changing server configuration.
9. A “Review required” state after a materially changed provider id, model, residency, retention,
   purpose set, or policy scope. Existing authorization must not be silently reused when its declared
   deployment assumptions no longer match.
10. Accessible, localized explanations and keyboard-operable controls. Color must never be the only
    signal for allowed, blocked, local, external, or sensitive states.

### Per-request preflight and informed confirmation

Pressing Generate in Query Studio should first perform a local, non-provider preflight:

1. Classify the question server-side and retain only safe detection reason codes.
2. Build the same bounded retrieved context that generation would use.
3. Resolve the effective tenant/DataSource provider policy and its scope.
4. Evaluate the complete content manifest without invoking the provider.
5. Return a disclosure summary containing:
   - provider and exact model;
   - purpose and DataSource;
   - policy id/scope and authorization ceiling;
   - declared, detected, and effective question classification;
   - residency and retention claims;
   - counts by content kind and classification;
   - included/redacted item identifiers or locally displayable labels;
   - whether a semantic retry can cause a second call;
   - an explicit `provider_invoked=false` preflight status.
6. Render a compact summary next to Generate, with an expandable exact-manifest view. The initial
   view should say, for example, “1 question, 2 tables, 14 columns, 1 relationship; no rows and no
   credentials.”
7. Require an explicit “Confirm and send to OpenAI/Claude/etc.” action when external egress is
   allowed. Local/private providers still receive a disclosure but may use organization-configured
   confirmation rules.

The confirmation must be bound to the actual generation request. A short-lived, single-use preflight
token or server-side signature should cover tenant, actor, DataSource, provider, model, purpose,
catalog version, effective policy id/version, question digest, privacy mode, and content-manifest
digest. Generation must reject stale confirmation after schema drift, policy changes, question
edits, provider/model changes, or context changes. Raw question text and parameter values must not
be added to the token, audit event, or logs.

### Query Studio persistent privacy indicators

Query Studio must always show, before generation:

- whether the selected provider is external cloud or local/private;
- the exact provider and model;
- the effective policy scope and maximum authorized classification;
- the current question's declared and effective classification;
- a persistent “database rows are not sent to the AI” statement;
- a warning that values typed directly into the question form part of the prompt;
- the number of provider calls allowed by the selected privacy mode;
- a direct link to review the effective policy without losing the current draft.

After generation it should show an AI transfer receipt indicating whether a call occurred, which
manifest counts were sent/redacted, the request id, provider/model, policy scope, latency, token
usage, and cost linkage. It must not display or persist a reconstructed full prompt.

### Structured, safe, and actionable error contract

Replace generic provider-policy strings with a stable structured `403` response. The response should
contain at least:

```json
{
  "code": "required_prompt_content_redacted",
  "provider_invoked": false,
  "provider_id": "openai",
  "purpose": "sql_proposal_generation",
  "policy_scope": "data_source",
  "declared_classification": "internal",
  "detected_classification": "pii",
  "effective_classification": "pii",
  "maximum_allowed_classification": "internal",
  "detection_reason_codes": ["phone_number"],
  "redacted_required_items": [
    {"id": "__request.question", "kind": "user_question"}
  ],
  "next_actions": ["remove_sensitive_literal", "review_provider_policy"]
}
```

The payload must never include the matched sensitive substring, prompt text, credentials, raw
parameters, or redacted schema descriptions. Equivalent structured codes are required for missing
policy, denied provider, denied purpose, residency mismatch, retention mismatch, all-content
redaction, stale preflight, and provider unavailability.

The user-facing message should lead with the outcome:

> Request not sent to OpenAI. The question was classified as PII because it contains a possible
> phone number, while the effective DataSource policy permits Internal content only.

It should then offer safe actions: remove the literal, replace it with a governed parameter, correct
an intentionally over-conservative classification through an authorized workflow if one exists, or
ask a security administrator to review the policy. “Raise the policy to Highly sensitive” must not
be presented as the default fix.

### Audit and AI activity receipts

Preserve content-minimizing audit while making authorization reviewable. Record:

- actor, tenant, DataSource, provider, model, purpose, privacy mode, and timestamp;
- effective policy id/scope/version or update timestamp;
- declared/detected/effective classification and safe reason codes;
- counts by content kind/classification and included/redacted counts;
- preflight digest, confirmation outcome, provider-invoked boolean, and terminal decision code;
- existing usage-event/cost linkage after a successful call.

Do not record question text, prompt text, matched sensitive values, schema descriptions, corrected
example text, raw parameter values, database rows, credentials, or provider responses in the privacy
receipt. Existing governed artifacts may retain their current deliberately modeled content; the
receipt must reference them by opaque ids rather than copy them.

### API and backend work

1. Add a non-provider preflight endpoint for SQL proposal generation and reusable policy-evaluation
   primitives for other LLM purposes.
2. Return effective policy, precedence, deployment metadata, and manifest summaries through explicit
   response models rather than frontend inference.
3. Propagate `ClassificationAssessment` reason codes and effective level to the policy boundary
   without propagating matched values.
4. Replace string-only `PromptEgressBlockedError` handling with typed error codes and safe structured
   metadata.
5. Bind confirmed preflight state to generation and reject time-of-check/time-of-use drift.
6. Expose a read-only effective-policy endpoint for a tenant/provider/DataSource combination.
7. Keep provider invocation strictly after successful policy evaluation and valid confirmation;
   blocked and preview-only paths must be testably network-free.
8. Version or otherwise bind policy acknowledgements when provider deployment metadata changes.
9. Maintain backward-compatible machine-readable error detail during a documented API transition;
   do not make clients parse localized prose.

### Migration and rollout

- Existing deny policies remain deny policies. No migration may convert a missing or denied policy
  into an allow policy.
- Existing tenant and DataSource allow policies remain technically effective, but the UI should mark
  them “review required” until their disclosure is acknowledged through the new experience.
- DataSource precedence remains unchanged and is made visible rather than flattened.
- Query drafts may survive navigation to the privacy step, but no raw parameter value or API key may
  be persisted in browser storage.
- Feature rollout should support a short compatibility period for API clients while the browser UI
  requires preflight confirmation immediately.
- Documentation, quickstart, screenshots, and live-certification instructions must describe the
  egress setup before the first provider-backed operation.

### Acceptance criteria

1. A new user cannot reach a provider-backed Generate action without seeing the effective sharing
   policy and what the operation may transmit.
2. Missing policy is presented as an incomplete setup prerequisite, not as a late technical error.
3. Every external-provider action has a local dry-run disclosure; previewing never calls a provider.
4. The disclosure identifies provider, exact model, purpose, policy scope, classification ceiling,
   residency claim, retention claim, manifest counts, and possible call count.
5. The UI states that database rows and credentials are not sent and separately warns that literals
   typed into the question are sent when allowed.
6. A DataSource override is visibly identified as taking precedence over the tenant baseline.
7. `provider_default` and `unspecified` cannot be mistaken for zero retention or guaranteed regional
   residency.
8. A server classification elevation displays safe reason codes and both declared and effective
   levels without echoing the matched value.
9. Every policy block explicitly states `provider_invoked=false`; an injected provider client proves
   zero calls on every blocked/preflight-only path.
10. Editing the question, changing provider/model/privacy mode, updating policy, or changing catalog
    version invalidates prior confirmation.
11. Maximum-privacy mode discloses one provider attempt; governed-semantic mode discloses the bounded
    possibility of a second attempt.
12. Successful generation produces a content-minimizing transfer receipt linked to existing usage
    and cost telemetry without persisting the prompt.
13. EXPLAIN, approval, execution, cancellation, masking, and result rendering remain provider-free.
14. The workflow is keyboard accessible, screen-reader labeled, localized at least for the existing
    Italian UI copy, and does not rely on color alone.
15. No regression weakens AST validation, read-only enforcement, RBAC, provider policy, FinOps,
    result privacy, audit minimization, or secret handling.

### Required verification

- Unit tests for classification precedence, safe reason-code propagation, manifest summaries,
  policy precedence, deployment metadata mismatches, and preflight-token invalidation.
- API tests for every structured block code, successful dry run, confirmation/generation binding,
  stale confirmation, RBAC, tenant isolation, and content-free error/log bodies.
- Browser tests covering first-run onboarding, missing-policy routing, tenant versus source policy,
  question edits, external/local provider labels, semantic-retry disclosure, and accessible focus/
  error behavior.
- Provider-spy tests asserting zero outbound calls for preflight, missing/denied/mismatched policy,
  redacted required content, stale confirmation, invalid RBAC, and budget failure.
- Persistence tests proving that receipts omit prompt text, matched values, raw parameters, rows,
  credentials, and provider response content.
- Regression tests proving EXPLAIN/approval/execution remain local and that existing policy and SQL
  safety behavior is unchanged.
- Documentation review by security/privacy stakeholders before calling the increment complete.

### Explicit non-goals

- This increment does not authorize result rows or narrative result interpretation to leave SQLVerity AI.
- It does not infer contractual residency or retention guarantees from provider names, API keys, or
  models.
- It does not add a global “allow everything” shortcut or allow the client to lower server-detected
  classification.
- It does not persist full prompts for convenience, debugging, history, or receipts.
- It does not replace organizational privacy review, data-processing agreements, provider account
  configuration, or deployment-specific legal controls.
