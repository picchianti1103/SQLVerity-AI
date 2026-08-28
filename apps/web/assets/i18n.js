"use strict";

(() => {
  const messages = Object.freeze({
    "app.skip": "Skip to content",
    "app.notConnected": "Not connected",
    "app.connectedApi": "Connected · API key",
    "app.connectedSso": "Connected · SSO",
    "app.notSelected": "not selected",
    "app.wait": "Please wait…",
    "app.httpError": "HTTP error {status}",
    "app.operationFailed": "Operation failed",
    "app.providerNotInvoked": "The request was not sent to the provider. ",
    "app.selectTenant": "Select a tenant",
    "app.noTenants": "No tenants are visible",
    "app.selectTenantSource": "Select a tenant and data source first.",
    "app.noData": "Not available",
    "app.yes": "yes",
    "app.no": "no",
    "app.none": "none",
    "app.close": "Close",
    "app.cancel": "Cancel",
    "connection.required": "Connect the console with an API key or SSO first.",
    "connection.csrfMissing": "The SSO session CSRF token is missing.",
    "connection.restricted": "Access is valid and limited to the assigned tenant.",
    "connection.ready": "Console connected. Select or create a tenant.",
    "connection.removed": "The session was removed from page memory.",
    "privacy.notSent": "Provider not invoked",
    "privacy.preflightAllowed": "Review before confirming the transfer",
    "privacy.preflightBlocked": "Request blocked locally",
    "privacy.preflightBinding": "The confirmation applies only to the question, context, model, and policy shown here.",
    "privacy.preflightSafeActions": "Safe next steps: remove sensitive literals, use governed parameters, or ask an administrator to review the policy and deployment.",
    "privacy.dryRunSuccess": "Dry run completed without contacting the provider. Review and confirm the manifest.",
    "privacy.dryRunBlocked": "Dry run completed: the request was blocked and the provider was not invoked.",
    "privacy.cancelled": "Transfer cancelled; the provider was not contacted.",
    "privacy.confirmAndSend": "2 · Send to {provider} and generate SQL",
    "query.generated": "Transfer confirmed, proposal generated and validated. No query was executed.",
    "query.generatedSemantic": "Transfer confirmed; the disclosed semantic retry was used and the proposal was validated.",
    "query.explainComplete": "EXPLAIN completed without executing the query.",
    "query.approved": "Query approved. Execution remains a separate manual action.",
    "query.executed": "Read-only execution completed; the result was processed locally.",
    "help.open": "Help and guidance",
    "help.search": "Search help topics",
    "help.noResults": "No help topics match this search.",
    "help.context": "Explain this page",
    "onboarding.title": "Getting started",
    "onboarding.subtitle": "Follow the governed path to your first result.",
    "onboarding.complete": "Setup complete",
    "onboarding.progress": "{complete} of {total} steps complete",
  });

  function interpolate(template, values) {
    return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, key) => (
      Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : match
    ));
  }

  function t(key, values = {}) {
    const template = messages[key];
    if (template === undefined) {
      console.warn(`Missing English UI message: ${key}`);
      return key;
    }
    return interpolate(template, values);
  }

  function apply(root = document) {
    root.querySelectorAll("[data-i18n]").forEach((element) => {
      element.textContent = t(element.dataset.i18n);
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
      element.setAttribute("placeholder", t(element.dataset.i18nPlaceholder));
    });
    root.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
      element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel));
    });
  }

  globalThis.SQLVerityI18n = Object.freeze({locale: "en", messages, t, apply});
})();
