"use strict";

(() => {
  const topics = Object.freeze([
    {
      id: "system",
      panel: "system",
      title: "Connect and choose your workspace",
      summary: "Authentication establishes who you are; the tenant and data source establish what you may work with.",
      points: [
        "API keys remain in page memory and disappear when the page is reloaded.",
        "SSO sessions are server-managed and protected with CSRF controls.",
        "Selecting a tenant never grants a role that your identity does not already have.",
      ],
      next: "Connect, then select or create the tenant you are authorized to use.",
    },
    {
      id: "sources",
      panel: "sources",
      title: "Register a governed data source",
      summary: "A data source describes how SQLVerity AI may discover metadata and run approved read-only operations.",
      points: [
        "Store only an opaque secret reference; never paste a database password into the catalog.",
        "Setup mode selects how the source is governed; database permissions separately control introspection, EXPLAIN, execution, and cancellation.",
        "Unavailable acquisition tabs are disabled because the selected source mode or permissions do not allow them.",
        "DDL import parses definitions without executing them, while Manual JSON imports a validated metadata snapshot.",
      ],
      next: "Register the source, then populate its versioned catalog by introspection or import.",
    },
    {
      id: "schema",
      panel: "schema",
      title: "Review schema and classifications",
      summary: "The current catalog version is the governed context used for retrieval, validation, and privacy decisions.",
      points: [
        "Classifications control which metadata may be sent to an AI provider.",
        "Relationships help SQLVerity AI retrieve connected tables without exposing database rows.",
        "Confirmed semantic descriptions take precedence over inferred descriptions.",
      ],
      next: "Inspect sensitive columns and relationships before authorizing AI sharing.",
    },
    {
      id: "privacy",
      panel: "privacy",
      title: "Control Privacy and AI Sharing",
      summary: "Provider credentials do not authorize data transfer. An effective, acknowledged egress policy is also required.",
      points: [
        "A DataSource policy overrides the tenant baseline only for that provider and source.",
        "Unspecified residency and provider-default retention are missing guarantees, not privacy assurances.",
        "SQLVerity AI never sends database rows, credentials, raw query parameters, EXPLAIN plans, or results to the AI provider.",
      ],
      next: "Verify the exact provider, model, purpose, classification ceiling, residency, and retention before saving.",
    },
    {
      id: "preflight",
      panel: "query",
      title: "Understand the AI transfer preflight",
      summary: "The preflight evaluates the exact production manifest locally and does not invoke the provider.",
      points: [
        "The full user question is part of the prompt, so values typed into it can be transmitted.",
        "A confirmation is short-lived, single-use, and bound to the actor, question, catalog, context, model, and policy.",
        "Governed semantic mode may make a second call using the same already-filtered context.",
      ],
      next: "Review included and redacted counts, then confirm only if the disclosure matches your intent.",
    },
    {
      id: "query",
      panel: "query",
      title: "Generate, validate, approve, and execute",
      summary: "Generation, validation, EXPLAIN, approval, and execution are separate trust boundaries.",
      points: [
        "A generated proposal is never executed automatically.",
        "AST validation limits SQL to the retrieved catalog context and blocks unsafe statements.",
        "The visible path is local sharing check, AI proposal, database plan, human approval, and read-only local execution.",
        "EXPLAIN inspects the database plan without running the query; it is different from the contextual page explanation.",
      ],
      next: "Read the interpretation and SQL, inspect validation and EXPLAIN, then approve and execute separately.",
    },
    {
      id: "results",
      panel: "query",
      title: "Interpret local results and provenance",
      summary: "Result formatting, masking, and summaries are deterministic and remain local.",
      points: [
        "Output lineage links each result column to governed source columns where it can be proven.",
        "Incomplete lineage triggers conservative masking rather than a silent privacy downgrade.",
        "Provenance records the source, catalog version, SQL, approval, execution metadata, and linked usage identifiers.",
      ],
      next: "Review the privacy report and provenance before using or sharing the result.",
    },
    {
      id: "administration",
      panel: "admin",
      title: "Administer identities and operations",
      summary: "Administrative actions are role-protected and remain separate from first-use privacy authorization.",
      points: [
        "Provision immutable OIDC subjects before federated users can access a tenant.",
        "DataSource-scoped roles narrow access and do not expand tenant authority.",
        "Choose the semantic-inference provider explicitly; its policy must authorize schema description inference.",
        "Connection tests and background jobs are audited and never return credentials.",
      ],
      next: "Use the narrowest role and scope required for each identity or operation.",
    },
    {
      id: "glossary",
      panel: "system",
      title: "Governance glossary",
      summary: "Common SQLVerity AI terms and why they matter.",
      points: [
        "Catalog version: an immutable snapshot of schema metadata used to bind retrieval and query validation.",
        "Classification ceiling: the highest sensitivity level an effective provider policy may transfer.",
        "Effective policy: the DataSource override when present; otherwise the tenant baseline.",
        "Transfer receipt: append-only, content-minimizing evidence of a preflight or provider-call outcome.",
        "Output lineage: the proven mapping from result columns back to governed source columns.",
      ],
      next: "Open contextual help from any page to see how these terms apply there.",
    },
  ]);

  const onboarding = Object.freeze([
    {id: "connected", label: "Connect securely", description: "Use an API key held only in memory or an SSO browser session.", panel: "system", help: "system"},
    {id: "tenant", label: "Choose a tenant", description: "Set the organizational boundary for every later operation.", panel: "system", help: "system"},
    {id: "source", label: "Select a data source", description: "Choose the database or governed metadata surface to query.", panel: "sources", help: "sources"},
    {id: "schema", label: "Load and review the schema", description: "Inspect the current catalog version and classifications.", panel: "schema", help: "schema"},
    {id: "privacy", label: "Authorize AI sharing", description: "Review and acknowledge the effective provider policy.", panel: "privacy", help: "privacy"},
    {id: "preflight", label: "Review an AI preflight", description: "Inspect the exact provider-free transfer disclosure.", panel: "query", help: "preflight"},
    {id: "proposal", label: "Generate the first proposal", description: "Confirm, validate, EXPLAIN, approve, and execute separately.", panel: "query", help: "query"},
  ]);

  globalThis.SQLVerityGuidance = Object.freeze({topics, onboarding});
})();
