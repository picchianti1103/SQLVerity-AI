"use strict";

const state = {
  token: "",
  authMode: "none",
  browserSession: null,
  connected: false,
  discovery: null,
  capabilities: null,
  tenants: [],
  tenantId: "",
  tenantName: "",
  sources: [],
  sourceId: "",
  source: null,
  schema: null,
  selectedObjectId: "",
  queryRun: null,
  privacyProviders: [],
  preflight: null,
  pendingForceSemantic: false,
  activeHelpTopic: "system",
  preflightReviewed: false,
  flashTimer: null,
};

const requests = new globalThis.SQLVerityRequests(api);

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const t = (key, values = {}) => globalThis.SQLVerityI18n.t(key, values);

class APIError extends Error {
  constructor(status, detail, structured = null) {
    super(detail);
    this.name = "APIError";
    this.status = status;
    this.structured = structured;
  }
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function errorDetail(payload, fallback) {
  if (!payload) return fallback;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) {
    return payload.detail
      .map((item) => `${Array.isArray(item.loc) ? item.loc.join(".") : "input"}: ${item.msg}`)
      .join(" · ");
  }
  if (payload.detail && typeof payload.detail === "object") {
    return payload.detail.message || payload.detail.code || fallback;
  }
  return fallback;
}

async function api(path, options = {}) {
  if (!state.token && state.authMode !== "oidc") {
    throw new APIError(401, t("connection.required"));
  }
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (!state.token && state.authMode === "oidc" && !["GET", "HEAD", "OPTIONS"].includes(options.method || "GET")) {
    const csrfToken = readCookie("sqlverity_csrf");
    if (!csrfToken) throw new APIError(403, t("connection.csrfMissing"));
    headers.set("X-CSRF-Token", csrfToken);
  }
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await fetch(path, {...options, headers});
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const structured = payload && payload.detail && typeof payload.detail === "object"
      ? payload.detail
      : null;
    throw new APIError(
      response.status,
      errorDetail(payload, t("app.httpError", {status: response.status})),
      structured,
    );
  }
  return payload;
}

function readCookie(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split(";").map((value) => value.trim()).find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

function showFlash(message, kind = "success") {
  const flash = $("#flash");
  flash.textContent = message;
  flash.dataset.kind = kind;
  flash.hidden = false;
  if (state.flashTimer !== null) window.clearTimeout(state.flashTimer);
  state.flashTimer = window.setTimeout(() => {
    flash.hidden = true;
    state.flashTimer = null;
  }, 6500);
}

function handleError(error, prefix = t("app.operationFailed")) {
  if (error?.name === "AbortError") return;
  const detail = error instanceof Error ? error.message : String(error);
  const notSent = error instanceof APIError && error.structured?.provider_invoked === false
    ? t("app.providerNotInvoked")
    : "";
  showFlash(`${prefix}: ${notSent}${detail}`, "error");
}

function setBusy(button, busy, busyLabel = t("app.wait")) {
  if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? busyLabel : button.dataset.idleLabel;
}

function switchPanel(name) {
  $$(".nav-item").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.panel === name);
  });
  $$('[data-panel-view]').forEach((panel) => {
    const active = panel.dataset.panelView === name;
    panel.classList.toggle("is-active", active);
    panel.hidden = !active;
  });
  $("#workspace").focus({preventScroll: true});
}

function currentPanel() {
  return $(".nav-item.is-active")?.dataset.panel || "system";
}

function helpTopics(search = "") {
  const normalized = search.trim().toLocaleLowerCase("en");
  const topics = globalThis.SQLVerityGuidance.topics;
  if (!normalized) return topics;
  return topics.filter((topic) => (
    `${topic.title} ${topic.summary} ${topic.points.join(" ")} ${topic.next}`
      .toLocaleLowerCase("en")
      .includes(normalized)
  ));
}

function renderHelpTopic(topicId) {
  const topics = globalThis.SQLVerityGuidance.topics;
  const topic = topics.find((item) => item.id === topicId) || topics[0];
  state.activeHelpTopic = topic.id;
  $$(".help-topic-button").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.helpTopic === topic.id);
  });
  const content = $("#help-content");
  content.replaceChildren();
  content.append(node("h3", "", topic.title));
  content.append(node("p", "", topic.summary));
  const points = node("ul");
  topic.points.forEach((point) => {
    points.append(node("li", "", point));
  });
  content.append(points);
  const next = node("p", "help-next");
  next.append(node("strong", "", "Recommended next step: "));
  next.append(document.createTextNode(topic.next));
  content.append(next);
}

function renderHelpTopics(search = "") {
  const container = $("#help-topic-list");
  container.replaceChildren();
  const topics = helpTopics(search);
  if (!topics.length) {
    container.append(node("p", "field-help", t("help.noResults")));
    $("#help-content").replaceChildren();
    return;
  }
  topics.forEach((topic) => {
    const button = node("button", "help-topic-button", topic.title);
    button.type = "button";
    button.dataset.helpTopic = topic.id;
    button.addEventListener("click", () => renderHelpTopic(topic.id));
    container.append(button);
  });
  const selected = topics.some((topic) => topic.id === state.activeHelpTopic)
    ? state.activeHelpTopic
    : topics[0].id;
  renderHelpTopic(selected);
}

function openHelp(topicId = "") {
  const contextualTopic = globalThis.SQLVerityGuidance.topics.find(
    (topic) => topic.panel === currentPanel(),
  );
  state.activeHelpTopic = topicId || contextualTopic?.id || "system";
  $("#help-search").value = "";
  renderHelpTopics();
  $("#help-backdrop").hidden = false;
  const drawer = $("#help-drawer");
  drawer.hidden = false;
  drawer.setAttribute("aria-hidden", "false");
  $("#help-button").setAttribute("aria-expanded", "true");
  document.body.classList.add("help-open");
  $("#close-help").focus({preventScroll: true});
}

function closeHelp() {
  $("#help-backdrop").hidden = true;
  const drawer = $("#help-drawer");
  drawer.hidden = true;
  drawer.setAttribute("aria-hidden", "true");
  $("#help-button").setAttribute("aria-expanded", "false");
  document.body.classList.remove("help-open");
  $("#help-button").focus({preventScroll: true});
}

function onboardingStatus() {
  const effectivePolicy = state.privacyProviders.some((item) => (
    item.decision_code === "allowed" && !item.review_required
  ));
  return {
    connected: state.connected,
    tenant: Boolean(state.tenantId),
    source: Boolean(state.sourceId),
    schema: Boolean(state.schema),
    privacy: effectivePolicy,
    preflight: state.preflightReviewed || Boolean(state.queryRun),
    proposal: Boolean(state.queryRun),
  };
}

function renderOnboarding() {
  const steps = globalThis.SQLVerityGuidance.onboarding;
  const status = onboardingStatus();
  const complete = steps.filter((step) => status[step.id]).length;
  const progress = $("#onboarding-progress");
  progress.style.width = `${Math.round((complete / steps.length) * 100)}%`;
  const track = progress.parentElement;
  track.setAttribute("aria-valuenow", String(complete));
  $("#onboarding-progress-label").textContent = complete === steps.length
    ? t("onboarding.complete")
    : t("onboarding.progress", {complete, total: steps.length});
  const list = $("#onboarding-list");
  list.replaceChildren();
  steps.forEach((step, index) => {
    const done = Boolean(status[step.id]);
    const item = node("button", `onboarding-item${done ? " is-complete" : ""}`);
    item.type = "button";
    item.dataset.onboardingStep = step.id;
    item.append(node("span", "onboarding-status", done ? "✓" : String(index + 1)));
    const copy = node("span", "onboarding-copy");
    copy.append(node("strong", "", step.label));
    copy.append(node("span", "", step.description));
    item.append(copy);
    item.append(node("span", "onboarding-action", done ? "Review" : "Open"));
    item.addEventListener("click", () => {
      switchPanel(step.panel);
      if (step.panel === "privacy" || step.panel === "query") loadPrivacyData();
      if (step.panel === "admin") loadAdminData();
    });
    list.append(item);
  });
}

function renderConnection() {
  const indicator = $("#connection-state");
  indicator.dataset.state = state.connected ? "online" : "offline";
  indicator.querySelector("span:last-child").textContent = state.connected
    ? (state.authMode === "oidc" ? t("app.connectedSso") : t("app.connectedApi"))
    : t("app.notConnected");
  $("#disconnect-button").disabled = !state.connected;
  $("#tenant-select").disabled = !state.connected;
  const sessionLabel = $("#browser-session");
  if (state.browserSession) {
    sessionLabel.textContent = `${state.browserSession.display_name} · MFA ${state.browserSession.mfa_verified ? "verified" : "not asserted"}`;
    sessionLabel.hidden = false;
  } else {
    sessionLabel.hidden = true;
  }
  renderOnboarding();
}

function renderTags(container, values, emptyLabel) {
  container.replaceChildren();
  if (!values || values.length === 0) {
    container.append(node("span", "tag muted", emptyLabel));
    return;
  }
  values.forEach((value) => container.append(node("span", "tag", value)));
}

function renderCapabilities() {
  const capabilities = state.capabilities;
  $("#service-version").textContent = capabilities ? `version ${capabilities.service_version}` : "version —";
  $("#catalog-backend").textContent = capabilities ? capabilities.catalog_backend : "—";
  renderTags(
    $("#dialect-list"),
    capabilities ? capabilities.supported_dialects : [],
    "not available",
  );
  const providers = capabilities ? capabilities.configured_provider_ids : [];
  renderTags($("#provider-list"), providers, "none configured or visible");
  const options = $("#provider-options");
  options.replaceChildren();
  providers.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider;
    options.append(option);
  });
  if (providers.length === 1) {
    $("#provider-select").value = providers[0];
    $("#inference-provider").value = providers[0];
  }
  if (providers.length === 0) $("#inference-provider").value = "";
}

function renderTenantOptions() {
  const select = $("#tenant-select");
  select.replaceChildren();
  select.append(new Option(state.tenants.length ? t("app.selectTenant") : t("app.noTenants"), ""));
  state.tenants.forEach((tenant) => select.append(new Option(tenant.name, tenant.id)));
  select.value = state.tenantId;
}

function updateContextHeader() {
  renderPermissions();
  $("#header-tenant").textContent = state.tenantName || state.tenantId || t("app.notSelected");
  $("#header-source").textContent = state.source ? state.source.name : t("app.notSelected");
  $("#import-target").textContent = state.source ? `${state.source.name} · ${state.source.dialect}` : "no data source";
  renderPolicyScopeSummary();
  renderOnboarding();
}

const sourceModeGuidance = {
  direct_db: {
    description: "Live database connection. Use schema introspection, inspect plans, and run explicitly approved read-only queries.",
    recommended: ["introspect", "explain", "execute_read_only", "cancel"],
  },
  hybrid: {
    description: "Live database plus imported metadata. It can use introspection, DDL import, and Manual JSON when the matching permissions are present.",
    recommended: ["introspect", "explain", "execute_read_only", "cancel"],
  },
  ddl_import: {
    description: "Offline catalog built from SQL definitions. The DDL is parsed and never executed against a database.",
    recommended: [],
  },
  manual_schema: {
    description: "Offline catalog built from a validated JSON snapshot. No database connection is required for acquisition.",
    recommended: [],
  },
  limited_schema: {
    description: "Advanced restricted-schema surface. This console can register it, but does not yet provide a dedicated object-restriction workflow.",
    recommended: [],
    warning: true,
  },
  view_source: {
    description: "Advanced governed-view surface. The views and least-privilege database access must already be configured outside this form.",
    recommended: [],
    warning: true,
  },
  metadata_file: {
    description: "Advanced file-backed metadata surface. This console does not yet provide a dedicated metadata-file upload workflow.",
    recommended: [],
    warning: true,
  },
  authorized_query: {
    description: "Advanced PostgreSQL virtual query surface. Register the reviewed base query through the API after creating this source.",
    recommended: ["explain", "execute_read_only"],
    warning: true,
  },
};

function renderSourceModeGuidance() {
  const mode = $("#source-type").value;
  const guidance = sourceModeGuidance[mode] || sourceModeGuidance.direct_db;
  const help = $("#source-mode-help");
  help.textContent = guidance.description;
  help.dataset.state = guidance.warning ? "warning" : "ready";
  const dialect = $("#source-dialect");
  const authorizedQuery = mode === "authorized_query";
  if (authorizedQuery) dialect.value = "postgresql";
  dialect.disabled = authorizedQuery;
  const introspection = $('input[name="capability"][value="introspect"]');
  introspection.disabled = authorizedQuery;
  introspection.closest("label").classList.toggle("is-disabled", authorizedQuery);
  if (authorizedQuery) introspection.checked = false;
  $("#capability-guidance").textContent = guidance.recommended.length
    ? `Suggested for this mode: ${guidance.recommended.join(", ").replaceAll("_", " ")}. Apply them only if the database account is intended to allow these operations.`
    : "No live database permission is required by this catalog mode. Add one only when the deployment design explicitly calls for it.";
}

function applyRecommendedSourceCapabilities() {
  const guidance = sourceModeGuidance[$("#source-type").value] || sourceModeGuidance.direct_db;
  const recommended = new Set(guidance.recommended);
  $$('input[name="capability"]').forEach((input) => {
    input.checked = !input.disabled && recommended.has(input.value);
  });
  showFlash("Recommended permissions applied. Review them before registering the source.");
}

function setImportTab(selected) {
  $$('[data-import-tab]').forEach((item) => {
    const active = !item.disabled && item.dataset.importTab === selected;
    item.classList.toggle("is-active", active);
    item.setAttribute("aria-selected", String(active));
  });
  $$('[data-import-view]').forEach((view) => {
    const active = view.dataset.importView === selected;
    view.classList.toggle("is-active", active);
    view.hidden = !active;
  });
}

function updateAcquisitionOptions() {
  const source = state.source;
  const capabilities = new Set(source?.capabilities || []);
  const allowed = {
    introspect: permitted("data_source.manage") && Boolean(source) && capabilities.has("introspect") && source.source_type !== "authorized_query",
    ddl: permitted("data_source.manage") && Boolean(source) && ["ddl_import", "hybrid"].includes(source.source_type),
    manual: permitted("data_source.manage") && Boolean(source) && ["manual_schema", "hybrid"].includes(source.source_type),
  };
  $$('[data-import-tab]').forEach((tab) => {
    tab.disabled = !allowed[tab.dataset.importTab];
    tab.setAttribute("aria-disabled", String(tab.disabled));
  });
  $("#run-introspection").disabled = !allowed.introspect;
  $("#ddl-form button[type='submit']").disabled = !allowed.ddl;
  $("#manual-form button[type='submit']").disabled = !allowed.manual;

  const available = Object.entries(allowed).filter(([, enabled]) => enabled).map(([method]) => method);
  const current = $("[data-import-tab].is-active")?.dataset.importTab || "";
  setImportTab(allowed[current] ? current : (available[0] || ""));
  const guidance = $("#acquisition-guidance");
  if (!source) {
    guidance.textContent = "Select a data source to see its compatible catalog methods.";
    guidance.dataset.state = "warning";
    return;
  }
  if (!available.length) {
    guidance.textContent = "This source has no catalog acquisition method available in the console. Complete its advanced setup through the API or register a source with compatible permissions.";
    guidance.dataset.state = "warning";
    return;
  }
  const labels = {introspect: "Introspection", ddl: "DDL import", manual: "Manual JSON"};
  guidance.textContent = `Available for ${source.name}: ${available.map((method) => labels[method]).join(", ")}. Unavailable methods are disabled because the source mode or permissions do not allow them.`;
  guidance.dataset.state = "ready";
}

async function connectConsole(event) {
  event.preventDefault();
  const button = event.submitter;
  const token = $("#api-token").value.trim();
  if (!token) return;
  resetConsoleSession();
  state.token = token;
  state.authMode = "api_key";
  state.browserSession = null;
  const op = requests.begin("connectConsole", "session");
  op.busy(button, "Connecting…");
  try {
    await loadAuthenticatedContext();
    op.check();
  } catch (error) {
    if (!op.current()) return;
    state.token = "";
    state.authMode = "none";
    state.connected = false;
    renderConnection();
    handleError(error, "Connection rejected");
  } finally {
    op.finish();
  }
}

function selectedTenant() {
  return state.discovery?.tenants.find((tenant) => tenant.id === state.tenantId);
}

function permitted(permission, tenantOnly = false) {
  if (state.discovery?.platform_permissions.includes("platform.manage")) return true;
  const tenant = selectedTenant();
  return Boolean(tenant?.permissions.includes(permission)
    || (!tenantOnly && state.source?.permissions?.includes(permission)));
}

function renderPermissions() {
  $("#tenant-form").hidden = !state.discovery?.platform_permissions.includes("platform.manage");
  $("#source-form").hidden = !permitted("data_source.manage", true);
  $("#federated-principal-form").hidden = !permitted("security.manage", true);
  $("#provider-policy-form").hidden = !permitted("security.manage");
  $("#test-connection").disabled = !permitted("data_source.manage");
  $("#enqueue-inference").disabled = !permitted("semantic.manage");
  $('[data-panel="query"]').disabled = !permitted("query.use");
  const scope = $("#policy-source-scope");
  scope.disabled = !permitted("security.manage", true);
  if (scope.disabled) scope.checked = true;
}

function clearContextResults() {
  $("#provider-policy-form").reset();
  $("#policy-deployment-summary").textContent = "Select a configured provider.";
  for (const id of ["#provider-select", "#inference-provider", "#policy-provider"]) $(id).value = "";
  for (const selector of ["#import-result", "#admin-operation-result", "#intent-correction-result"]) {
    $(selector).hidden = true;
    $(selector).replaceChildren();
  }
  renderAdminItems($("#principal-list"), [], "Select a tenant to load identities.", () => node("div"));
  renderAdminItems($("#job-list"), [], "Select a tenant to load jobs.", () => node("div"));
  state.privacyProviders = [];
  renderPrivacyProviders();
}

async function loadAuthenticatedContext() {
  const op = requests.begin("authenticated-context", "session");
  try {
    state.discovery = await op.api("/v1/session");
    state.tenants = state.discovery.tenants;
    state.capabilities = state.discovery.platform_permissions.includes("platform.manage")
      ? await op.api("/v1/system/capabilities") : null;
    state.connected = true;
    renderConnection();
    renderTenantOptions();
    renderCapabilities();
    renderPermissions();
    if (state.tenants.length === 1) {
      await selectTenant(state.tenants[0].id, state.tenants[0].name);
      op.check();
    }
    showFlash(t("connection.ready"));
  } catch (error) {
    if (!op.current()) return;
    throw error;
  }
}

function resetConsoleSession() {
  requests.invalidate("session");
  state.discovery = null;
  clearContextResults();
  state.token = "";
  state.authMode = "none";
  state.browserSession = null;
  state.connected = false;
  state.capabilities = null;
  state.tenants = [];
  state.tenantId = "";
  state.tenantName = "";
  state.sources = [];
  state.sourceId = "";
  state.source = null;
  state.schema = null;
  state.queryRun = null;
  state.privacyProviders = [];
  state.preflight = null;
  state.preflightReviewed = false;
  state.pendingForceSemantic = false;
  $("#api-token").value = "";
  renderConnection();
  renderCapabilities();
  renderTenantOptions();
  renderSources();
  resetSchema();
  resetQueryWorkflow();
  renderPrivacyProviders();
  renderQueryPrivacyStatus();
  updateContextHeader();
}

async function disconnectConsole() {
  const wasOIDC = state.authMode === "oidc";
  resetConsoleSession();
  const op = requests.begin("logout", "session");
  showFlash(t("connection.removed"));
  if (wasOIDC) {
    try {
      await op.wait(fetch("/auth/oidc/logout", {
        method: "POST", headers: {"X-CSRF-Token": readCookie("sqlverity_csrf")},
      }));
    } catch (error) {
      if (op.current()) handleError(error, "SSO sign-out failed");
    }
  }
}

async function initializeOIDC() {
  const op = requests.begin("initializeOIDC", "session");
  try {
    const configResponse = await op.wait(fetch("/auth/oidc/config", {headers: {"Accept": "application/json"}}));
    if (!configResponse.ok) return;
    const config = await op.wait(configResponse.json());
    const login = $("#oidc-login");
    login.hidden = !config.enabled;
    if (config.login_url) login.href = config.login_url;
    if (!config.enabled) return;
    const sessionResponse = await op.wait(fetch("/auth/oidc/session", {
      headers: {"Accept": "application/json"},
    }));
    if (!sessionResponse.ok) return;
    state.browserSession = await op.wait(sessionResponse.json());
    state.authMode = "oidc";
    state.connected = true;
    await loadAuthenticatedContext();
    op.check();
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Could not restore the SSO session");
  }
}

async function createTenant(event) {
  const op = requests.begin("createTenant", "tenant");
  event.preventDefault();
  const button = event.submitter;
  const name = $("#tenant-name").value.trim();
  op.busy(button, "Creating…");
  try {
    const tenant = await op.api("/v1/tenants", {
      method: "POST",
      body: JSON.stringify({name}),
    });
    state.tenants.push(tenant);
    $("#tenant-name").value = "";
    renderTenantOptions();
    await selectTenant(tenant.id, tenant.name);
    op.check();
    showFlash(`Tenant “${tenant.name}” created.`);
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Could not create the tenant");
  } finally {
    op.finish();
  }
}

async function selectTenant(id, name = "") {
  requests.invalidate("tenant");
  state.sources = [];
  renderSources();
  clearContextResults();
  state.tenantId = id;
  const knownTenant = state.tenants.find((tenant) => tenant.id === id);
  state.tenantName = name || (knownTenant ? knownTenant.name : id);
  state.sourceId = "";
  state.source = null;
  state.schema = null;
  state.queryRun = null;
  state.privacyProviders = [];
  state.preflightReviewed = false;
  invalidatePreflight();
  renderTenantOptions();
  updateContextHeader();
  resetSchema();
  resetQueryWorkflow();
  updateAcquisitionOptions();
  await loadSources();
}

async function loadSources() {
  if (!state.tenantId) {
    state.sources = [];
    renderSources();
    return;
  }
  const op = requests.begin("sources", "tenant");
  try {
    state.discovery = await op.api("/v1/session");
    state.tenants = state.discovery.tenants;
    state.sources = (selectedTenant()?.data_sources || []).map((entry) => ({
      ...entry.source, permissions: entry.permissions,
    }));
    if (state.sourceId) {
      state.source = state.sources.find((source) => source.id === state.sourceId) || null;
      if (!state.source) selectSource("");
    }
    renderTenantOptions();
    renderSources();
    updateContextHeader();
  } catch (error) {
    if (!op.current()) return;
    state.sources = [];
    selectSource("");
    renderSources();
    handleError(error, "Could not load data sources");
  }
}

function renderSources() {
  const list = $("#source-list");
  list.replaceChildren();
  list.classList.toggle("empty-state", state.sources.length === 0);
  if (state.sources.length === 0) {
    list.append(node("p", "", state.tenantId ? "No data sources registered." : "Select a tenant to view data sources."));
    renderOnboarding();
    updateAcquisitionOptions();
    return;
  }
  state.sources.forEach((source) => {
    const button = node("button", "source-item");
    button.type = "button";
    button.dataset.sourceId = source.id;
    button.classList.toggle("is-active", source.id === state.sourceId);
    button.append(node("strong", "", source.name));
    button.append(node("span", "", `${source.dialect} · ${source.source_type}`));
    button.addEventListener("click", () => selectSource(source.id));
    list.append(button);
  });
  renderOnboarding();
  updateAcquisitionOptions();
}

function selectSource(id) {
  if (id && !state.sources.some((source) => source.id === id)) return;
  requests.invalidate("source");
  clearContextResults();
  state.sourceId = id;
  state.source = state.sources.find((source) => source.id === id) || null;
  state.schema = null;
  state.queryRun = null;
  state.privacyProviders = [];
  state.preflightReviewed = false;
  invalidatePreflight();
  renderSources();
  updateContextHeader();
  resetSchema();
  resetQueryWorkflow();
  updateAcquisitionOptions();
  loadPrivacyData();
  showFlash(`Data source “${state.source ? state.source.name : id}” selected.`);
}

async function createSource(event) {
  const op = requests.begin("createSource", "source");
  event.preventDefault();
  if (!state.tenantId) {
    showFlash("Select a tenant first.", "error");
    return;
  }
  const button = event.submitter;
  const capabilities = $$('input[name="capability"]:checked').map((input) => input.value);
  const secretRef = $("#secret-ref").value.trim();
  const payload = {
    name: $("#source-name").value.trim(),
    source_type: $("#source-type").value,
    dialect: $("#source-dialect").value,
    capabilities,
    connection_secret_ref: secretRef || null,
  };
  op.busy(button, "Registering…");
  try {
    const source = await op.api(`/v1/tenants/${encodeURIComponent(state.tenantId)}/data-sources`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    event.target.reset();
    renderSourceModeGuidance();
    await loadSources();
    op.check();
    selectSource(source.id);
    showFlash(`Data source “${source.name}” registered. Populate its catalog next.`);
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Could not register the data source");
  } finally {
    op.finish();
  }
}

function requireSource() {
  if (!state.tenantId || !state.sourceId) {
    showFlash(t("app.selectTenantSource"), "error");
    return false;
  }
  return true;
}

function sourcePath(suffix) {
  return `/v1/tenants/${encodeURIComponent(state.tenantId)}/data-sources/${encodeURIComponent(state.sourceId)}${suffix}`;
}

function renderImportResult(result) {
  const box = $("#import-result");
  box.textContent = `Catalog v${result.catalog_version}: ${result.object_count} objects, ${result.column_count} columns, ${result.relationship_count} relationships.`;
  box.hidden = false;
  state.schema = null;
  resetSchema();
}

async function runIntrospection() {
  const op = requests.begin("runIntrospection", "source");
  if (!requireSource()) return;
  const button = $("#run-introspection");
  op.busy(button, "Introspecting…");
  try {
    const result = await op.api(sourcePath("/ingestions"), {method: "POST"});
    renderImportResult(result);
    showFlash("Introspection completed and a new catalog version was created.");
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Introspection failed");
  } finally {
    op.finish();
  }
}

async function importDDL(event) {
  const op = requests.begin("importDDL", "source");
  event.preventDefault();
  if (!requireSource()) return;
  const button = event.submitter;
  const defaultSchema = $("#default-schema").value.trim();
  op.busy(button, "Importing…");
  try {
    const result = await op.api(sourcePath("/imports/ddl"), {
      method: "POST",
      body: JSON.stringify({
        ddl: $("#ddl-input").value,
        default_schema: defaultSchema || null,
      }),
    });
    renderImportResult(result);
    showFlash("DDL parsed without execution; the catalog was updated.");
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "DDL import failed");
  } finally {
    op.finish();
  }
}

async function importManual(event) {
  const op = requests.begin("importManual", "source");
  event.preventDefault();
  if (!requireSource()) return;
  const button = event.submitter;
  let payload;
  try {
    payload = JSON.parse($("#manual-input").value);
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Invalid JSON");
    return;
  }
  op.busy(button, "Importing…");
  try {
    const result = await op.api(sourcePath("/imports/manual"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderImportResult(result);
    showFlash("The manual snapshot was validated and imported.");
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Manual import failed");
  } finally {
    op.finish();
  }
}

function resetSchema() {
  $("#schema-version").textContent = "catalog —";
  $("#schema-search").value = "";
  const objects = $("#object-list");
  objects.className = "object-list card empty-state";
  objects.replaceChildren(node("p", "", "No schema loaded."));
  const detail = $("#object-detail");
  detail.className = "card object-detail empty-state";
  detail.replaceChildren(node("p", "", "Select an object to inspect its details."));
  const relationships = $("#relationship-list");
  relationships.className = "relationship-list empty-state";
  relationships.replaceChildren(node("p", "", "No relationships loaded."));
  renderOnboarding();
}

async function loadSchema() {
  const op = requests.begin("loadSchema", "source");
  if (!requireSource()) return;
  const button = $("#refresh-schema");
  op.busy(button, "Loading…");
  try {
    state.schema = await op.api(sourcePath("/schema"));
    state.selectedObjectId = state.schema.objects.length ? state.schema.objects[0].id : "";
    renderSchema();
    showFlash(`Catalog schema v${state.schema.catalog_version} loaded.`);
  } catch (error) {
    if (!op.current()) return;
    resetSchema();
    handleError(error, "Schema unavailable");
  } finally {
    op.finish();
  }
}

function renderSchema() {
  if (!state.schema) {
    resetSchema();
    return;
  }
  $("#schema-version").textContent = `catalog v${state.schema.catalog_version}`;
  const search = $("#schema-search").value.trim().toLocaleLowerCase("en");
  const objects = state.schema.objects.filter((schemaObject) => {
    if (!search) return true;
    return schemaObject.reference.toLocaleLowerCase("en").includes(search)
      || schemaObject.columns.some((column) => column.name.toLocaleLowerCase("en").includes(search));
  });
  const list = $("#object-list");
  list.replaceChildren();
  list.className = `object-list card${objects.length ? "" : " empty-state"}`;
  if (!objects.length) {
    list.append(node("p", "", "No objects match the filter."));
  } else {
    objects.forEach((schemaObject) => {
      const button = node("button", "object-button");
      button.type = "button";
      button.classList.toggle("is-active", schemaObject.id === state.selectedObjectId);
      button.append(node("span", "object-kind", schemaObject.kind));
      button.append(node("span", "object-ref", schemaObject.reference));
      button.addEventListener("click", () => {
        state.selectedObjectId = schemaObject.id;
        renderSchema();
      });
      list.append(button);
    });
  }
  const selected = state.schema.objects.find((item) => item.id === state.selectedObjectId) || objects[0];
  if (selected) state.selectedObjectId = selected.id;
  renderObjectDetail(selected);
  renderRelationships(state.schema.relationships);
  renderOnboarding();
}

function renderObjectDetail(schemaObject) {
  const detail = $("#object-detail");
  detail.replaceChildren();
  if (!schemaObject) {
    detail.className = "card object-detail empty-state";
    detail.append(node("p", "", "No object selected."));
    return;
  }
  detail.className = "card object-detail";
  detail.append(node("span", "card-kicker", schemaObject.kind));
  detail.append(node("h2", "detail-title", schemaObject.reference));
  const semantic = schemaObject.semantics;
  detail.append(node(
    "p",
    "detail-description",
    semantic ? `${semantic.description} · ${semantic.status} · confidence ${semantic.confidence}` : "No confirmed semantic description.",
  ));
  const heading = node("div", "card-heading");
  const headingCopy = node("div");
  headingCopy.append(node("span", "card-kicker", "Columns"));
  headingCopy.append(node("h2", "", `${schemaObject.columns.length} fields`));
  heading.append(headingCopy);
  detail.append(heading);
  const columns = node("div", "column-list");
  schemaObject.columns.forEach((column) => {
    const row = node("div", "column-row");
    row.append(node("strong", "", column.name));
    row.append(node("span", "", column.physical_type));
    row.append(node("span", "classification", column.classification));
    row.append(node("span", "", column.is_primary_key ? "PK" : column.nullable ? "nullable" : "required"));
    columns.append(row);
  });
  detail.append(columns);
  if (schemaObject.definition_sql) {
    const disclosure = node("details", "provenance");
    const summary = node("summary", "", "Show SQL definition");
    const pre = node("pre", "code-output compact");
    pre.append(node("code", "", schemaObject.definition_sql));
    disclosure.append(summary, pre);
    detail.append(disclosure);
  }
}

function renderRelationships(relationships) {
  const list = $("#relationship-list");
  list.replaceChildren();
  list.className = `relationship-list${relationships.length ? "" : " empty-state"}`;
  if (!relationships.length) {
    list.append(node("p", "", "No relationships are declared in the current version."));
    return;
  }
  relationships.forEach((relationship) => {
    const item = node("div", "relationship-item");
    item.append(node("strong", "", relationship.name));
    item.append(node(
      "span",
      "",
      `${relationship.source_object_ref} (${relationship.source_columns.join(", ")}) → ${relationship.target_object_ref} (${relationship.target_columns.join(", ")})`,
    ));
    list.append(item);
  });
}

const classificationLabels = {
  public: "public",
  internal: "internal",
  confidential: "confidential",
  pii: "PII",
  highly_sensitive: "highly sensitive",
};

const manifestKindLabels = {
  user_question: "question",
  generation_constraint: "generation constraint",
  schema_object: "tables or views",
  schema_column: "columns",
  schema_relationship: "relationships",
  business_concept: "business concepts",
  metric_definition: "metrics",
  business_rule: "business rules",
  corrected_sql_example: "governed SQL examples",
};

const privacyDecisionLabels = {
  missing_policy: "No sharing policy exists for this provider.",
  denied_provider: "The effective policy denies this provider.",
  denied_purpose: "The policy does not authorize this purpose.",
  residency_mismatch: "The policy residency does not match the configured deployment.",
  retention_mismatch: "The policy retention does not match the configured deployment.",
  policy_review_required: "The deployment or policy changed and requires a new acknowledgement.",
  required_prompt_content_redacted: "Required content exceeds the maximum allowed classification.",
  all_prompt_content_redacted: "The policy would redact all required content.",
  invalid_policy_redaction: "The policy returned an unknown redaction identifier.",
};

function invalidatePreflight() {
  requests.invalidate("query");
  state.preflight = null;
  state.preflightReviewed = false;
  state.pendingForceSemantic = false;
  const card = $("#ai-preflight");
  if (card) card.hidden = true;
  renderOnboarding();
}

function selectPrivacyProvider(item) {
  $("#policy-provider").value = item.deployment.provider_id;
  $("#provider-select").value = item.deployment.provider_id;
  $("#inference-provider").value = item.deployment.provider_id;
  $("#policy-residency").value = item.deployment.data_residency;
  $("#policy-retention").value = item.deployment.retention_mode;
  $("#policy-deployment-summary").textContent = `${item.deployment.model_id} · ${item.deployment.deployment_type === "local_private" ? "local/private" : "external cloud"} · residency ${item.deployment.data_residency} · retention ${item.deployment.retention_mode}`;
  $("#policy-classification").value = "internal";
  $("#policy-source-scope").checked = false;
  $("#policy-allowed").checked = true;
  $$('.purpose-fieldset input[name="policy-purpose"]').forEach((input) => {
    input.checked = input.value === "sql_proposal_generation";
  });
  const policy = item.policy;
  if (policy) {
    $("#policy-classification").value = policy.maximum_classification;
    $("#policy-source-scope").checked = item.policy_scope === "data_source";
    $("#policy-allowed").checked = policy.allowed;
    $$('input[name="policy-purpose"]').forEach((input) => {
      input.checked = policy.allowed_purposes.includes(input.value);
    });
  }
  $("#policy-acknowledged").checked = false;
  $$(".provider-card").forEach((card) => {
    card.classList.toggle("is-active", card.dataset.providerId === item.deployment.provider_id);
  });
  renderPermissions();
  renderPolicyScopeSummary();
  invalidatePreflight();
  renderQueryPrivacyStatus();
}

function renderPrivacyProviders() {
  const container = $("#privacy-provider-list");
  const policies = $("#policy-list");
  container.replaceChildren();
  policies.replaceChildren();
  const empty = state.privacyProviders.length === 0;
  container.classList.toggle("empty-state", empty);
  policies.classList.toggle("empty-state", empty);
  if (empty) {
    const message = state.sourceId
      ? "No providers configured."
      : "Select a tenant and data source.";
    container.append(node("p", "", message));
    policies.append(node("p", "", message));
    return;
  }
  state.privacyProviders.forEach((item) => {
    const card = node("button", "provider-card");
    card.type = "button";
    card.dataset.providerId = item.deployment.provider_id;
    card.setAttribute("aria-label", `Configure ${item.deployment.provider_id}`);
    card.append(node("strong", "", `${item.deployment.provider_id} · ${item.deployment.model_id}`));
    card.append(node("span", "", item.deployment.deployment_type === "local_private" ? "Local or private" : "External cloud"));
    card.append(node("span", "", `Residency ${item.deployment.data_residency} · retention ${item.deployment.retention_mode}`));
    const outcome = item.decision_code === "allowed"
      ? `allowed · max ${item.policy.maximum_classification}`
      : privacyDecisionLabels[item.decision_code] || item.decision_code;
    const badge = node("span", "status-badge", outcome);
    badge.dataset.state = item.decision_code === "allowed" ? "ok" : "error";
    card.append(badge);
    card.addEventListener("click", () => selectPrivacyProvider(item));
    container.append(card);

    const policyItem = node("div", "admin-item");
    policyItem.append(node("strong", "", `${item.deployment.provider_id} · ${outcome}`));
    policyItem.append(node("span", "", `Effective scope: ${item.policy_scope}${item.policy_scope === "data_source" ? " · overrides the tenant baseline" : ""}`));
    if (item.policy) {
      policyItem.append(node("span", "", `Purposes: ${item.policy.allowed_purposes.join(", ")}`));
    }
    policies.append(policyItem);
  });
  renderOnboarding();
}

async function loadPrivacyData() {
  const op = requests.begin("loadPrivacyData", "source");
  if (!state.tenantId || !state.sourceId) {
    state.privacyProviders = [];
    renderPrivacyProviders();
    renderQueryPrivacyStatus();
    return;
  }
  try {
    state.privacyProviders = await op.api(sourcePath("/privacy/providers"));
    renderPrivacyProviders();
    if (state.privacyProviders.length === 1 && !$("#inference-provider").value.trim()) {
      $("#inference-provider").value = state.privacyProviders[0].deployment.provider_id;
    }
    const providerId = $("#provider-select").value.trim();
    const selected = state.privacyProviders.find((item) => item.deployment.provider_id === providerId);
    if (selected) selectPrivacyProvider(selected);
    renderQueryPrivacyStatus();
  } catch (error) {
    if (!op.current()) return;
    state.privacyProviders = [];
    renderPrivacyProviders();
    renderQueryPrivacyStatus();
    handleError(error, "Could not load privacy rules");
  }
}

function renderPolicyScopeSummary() {
  const summary = $("#policy-scope-summary");
  if ($("#policy-source-scope").checked) {
    summary.textContent = state.source
      ? `Source override: applies only to ${state.source.name} and takes precedence over the tenant baseline for this provider.`
      : "Source override: select a data source before saving this narrower policy.";
    summary.dataset.state = state.source ? "ready" : "warning";
    return;
  }
  summary.textContent = "Tenant baseline: applies to this provider across the current tenant unless a source override exists.";
  summary.dataset.state = "ready";
}

function renderQueryPrivacyStatus() {
  const providerId = $("#provider-select")?.value.trim() || "";
  const item = state.privacyProviders.find((entry) => entry.deployment.provider_id === providerId);
  const setup = $("#query-privacy-setup");
  const button = $("#preflight-button");
  const classification = $("#classification-select")?.value || "internal";
  const calls = $("#privacy-mode-select")?.value === "governed_semantic" ? 2 : 1;
  $("#query-classification-indicator").textContent = `Declared classification: ${classificationLabels[classification] || classification}`;
  $("#query-call-indicator").textContent = `AI calls: maximum ${calls}`;
  if (!item) {
    $("#query-provider-indicator").textContent = providerId ? `Provider: ${providerId} is not configured` : "Provider: —";
    $("#query-policy-indicator").textContent = "Policy: unavailable";
    setup.hidden = false;
    $("#query-privacy-setup-message").textContent = "Select a configured provider and create an effective policy before generation.";
    button.disabled = true;
    return;
  }
  const providerKind = item.deployment.deployment_type === "local_private" ? "local/private" : "external cloud";
  $("#query-provider-indicator").textContent = `Provider: ${item.deployment.provider_id} / ${item.deployment.model_id} · ${providerKind}`;
  const missing = !item.policy;
  const denied = item.policy && !item.policy.allowed;
  const purposeDenied = item.policy && !item.policy.allowed_purposes.includes("sql_proposal_generation");
  const deploymentMismatch = !item.deployment_matches_policy;
  const blocked = !permitted("query.use") || missing || denied || purposeDenied || item.review_required || deploymentMismatch;
  $("#query-policy-indicator").textContent = item.policy
    ? `Policy: ${item.policy_scope} · max ${item.policy.maximum_classification}${item.review_required ? " · review required" : ""}`
    : "Policy: missing";
  setup.hidden = !blocked;
  if (blocked) {
    $("#query-privacy-setup-message").textContent = missing
      ? "No sharing policy exists for this provider."
      : denied
        ? "The effective policy denies this provider."
        : purposeDenied
          ? "The policy does not authorize SQL proposal generation."
          : deploymentMismatch
            ? privacyDecisionLabels[item.decision_code] || "The policy does not match the configured deployment."
            : "The deployment or policy changed and requires a new acknowledgement.";
  }
  button.disabled = blocked;
}

function resetQueryWorkflow() {
  requests.invalidate("query");
  state.queryRun = null;
  $("#query-workflow").hidden = true;
  $("#explain-section").hidden = true;
  $("#result-section").hidden = true;
  $("#ai-transfer-receipt").hidden = true;
  $("#approve-button").disabled = true;
  $("#execute-button").disabled = true;
  $("#explain-button").disabled = false;
  updateSemanticRetryAvailability();
  setWorkflowStage("proposal");
}

function setWorkflowStage(stage) {
  const order = ["proposal", "explain", "approval", "execution"];
  const currentIndex = order.indexOf(stage);
  $$(".workflow-step").forEach((step) => {
    const index = order.indexOf(step.dataset.stage);
    step.classList.toggle("is-current", index === currentIndex);
    step.classList.toggle("is-complete", index < currentIndex);
  });
}

async function generateProposal(event) {
  event.preventDefault();
  if (!requireSource()) return;
  await requestAITransferPreflight(false, event.submitter);
}

function renderPreflight() {
  const preflight = state.preflight;
  const card = $("#ai-preflight");
  if (!preflight) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  $("#preflight-status").textContent = t("privacy.notSent");
  $("#preflight-status").dataset.state = preflight.allowed ? "ok" : "error";
  $("#preflight-title").textContent = preflight.allowed
    ? t("privacy.preflightAllowed")
    : t("privacy.preflightBlocked");
  const totals = new Map();
  preflight.content_counts.forEach((item) => {
    const current = totals.get(item.kind) || {included: 0, redacted: 0};
    current.included += item.included_count;
    current.redacted += item.redacted_count;
    totals.set(item.kind, current);
  });
  const summary = Array.from(totals.entries())
    .filter(([, count]) => count.included > 0)
    .map(([kind, count]) => `${count.included} ${manifestKindLabels[kind] || kind}`)
    .join(", ");
  $("#preflight-summary").textContent = preflight.allowed
    ? `${summary || "Governed context"}; no database rows or credentials. The provider has not been invoked.`
    : `Request not sent to ${preflight.provider_id}. ${privacyDecisionLabels[preflight.decision_code] || `Decision: ${preflight.decision_code}.`} The provider was not invoked.`;
  $("#preflight-actions").textContent = preflight.allowed
    ? t("privacy.preflightBinding")
    : t("privacy.preflightSafeActions");
  const meta = $("#preflight-meta");
  meta.replaceChildren();
  appendMeta(meta, "Provider and model", `${preflight.provider_id} / ${preflight.model_id}`);
  appendMeta(meta, "Deployment", preflight.deployment_type === "local_private" ? "local/private" : "external cloud");
  appendMeta(meta, "Effective policy", `${preflight.policy_scope} · max ${preflight.maximum_allowed_classification}`);
  appendMeta(meta, "Classification", `declared ${preflight.declared_classification} · detected ${preflight.detected_classification} · effective ${preflight.effective_classification}`);
  appendMeta(meta, "Safe reasons", preflight.detection_reason_codes);
  appendMeta(meta, "Residency", preflight.data_residency === "unspecified" ? "not guaranteed in SQLVerity AI" : preflight.data_residency);
  appendMeta(meta, "Retention", preflight.retention_mode === "provider_default" ? "provider/account default; not zero retention" : preflight.retention_mode);
  appendMeta(meta, "Allowed calls", preflight.maximum_provider_calls);
  const manifest = $("#preflight-manifest");
  manifest.replaceChildren();
  preflight.content_counts.forEach((item) => {
    const row = node("div", "manifest-item");
    row.append(node("strong", "", manifestKindLabels[item.kind] || item.kind));
    row.append(node("span", "", `${item.classification} · included ${item.included_count} · redacted ${item.redacted_count}`));
    manifest.append(row);
  });
  const ids = node("p", "field-help", `Included: ${preflight.included_content_ids.join(", ") || "none"}. Redacted: ${preflight.redacted_content_ids.join(", ") || "none"}.`);
  manifest.append(ids);
  const confirm = $("#confirm-ai-transfer");
  confirm.textContent = t("privacy.confirmAndSend", {provider: preflight.provider_id});
  confirm.disabled = !preflight.allowed || preflight.review_required || !preflight.confirmation_token;
}

async function requestAITransferPreflight(forceSemantic, button) {
  if (!requireSource()) return;
  if (!forceSemantic) resetQueryWorkflow();
  invalidatePreflight();
  const op = requests.begin("requestAITransferPreflight", "query");
  op.busy(button, "Running local check…");
  try {
    const semanticOptions = forceSemantic
      ? {privacy_mode: "governed_semantic", force_semantic: true}
      : {privacy_mode: $("#privacy-mode-select").value, force_semantic: false};
    state.preflight = await op.api(sourcePath("/sql/preflights"), {
      method: "POST",
      body: JSON.stringify({
        provider_id: $("#provider-select").value.trim(),
        query: forceSemantic && state.queryRun
          ? state.queryRun.context.query
          : $("#question-input").value.trim(),
        question_classification: $("#classification-select").value,
        ...semanticOptions,
      }),
    });
    state.pendingForceSemantic = forceSemantic;
    state.preflightReviewed = true;
    renderPreflight();
    renderOnboarding();
    showFlash(state.preflight.allowed
      ? t("privacy.dryRunSuccess")
      : t("privacy.dryRunBlocked"), state.preflight.allowed ? "success" : "error");
  } catch (error) {
    if (!op.current()) return;
    invalidatePreflight();
    handleError(error, "Privacy preflight failed");
  } finally {
    op.finish();
  }
}

function renderTransferReceipt(receipt) {
  const card = $("#ai-transfer-receipt");
  if (!receipt) {
    card.hidden = true;
    return;
  }
  const meta = $("#ai-transfer-receipt-meta");
  meta.replaceChildren();
  appendMeta(meta, "Receipt", receipt.id);
  appendMeta(meta, "Provider", `${receipt.provider_id} / ${receipt.model_id}`);
  appendMeta(meta, "Policy", `${receipt.policy_scope} · ${receipt.provider_policy_id || "none"}`);
  appendMeta(meta, "Effective classification", receipt.effective_classification);
  appendMeta(meta, "Outcome", `${receipt.decision_code} · provider invoked: ${receipt.provider_invoked ? "yes" : "no"}`);
  appendMeta(meta, "Telemetry", `${receipt.input_tokens ?? "—"} input tokens · ${receipt.output_tokens ?? "—"} output tokens · ${receipt.latency_ms ?? "—"} ms`);
  appendMeta(meta, "Cost", `estimated ${receipt.estimated_cost ?? "—"} · actual ${receipt.actual_cost ?? "—"}`);
  appendMeta(meta, "Usage/cost link", receipt.llm_usage_event_id || "not available");
  card.hidden = false;
}

async function confirmAITransfer() {
  const op = requests.begin("confirmAITransfer", "query");
  if (!state.preflight || !state.preflight.confirmation_token || !requireSource()) return;
  const button = $("#confirm-ai-transfer");
  op.busy(button, "Sending…");
  try {
    state.queryRun = await op.api(sourcePath("/sql/proposals"), {
      method: "POST",
      body: JSON.stringify({
        provider_id: $("#provider-select").value.trim(),
        query: state.pendingForceSemantic && state.queryRun
          ? state.queryRun.context.query
          : $("#question-input").value.trim(),
        question_classification: $("#classification-select").value,
        privacy_mode: state.pendingForceSemantic ? "governed_semantic" : $("#privacy-mode-select").value,
        force_semantic: state.pendingForceSemantic,
        confirmation_token: state.preflight.confirmation_token,
      }),
    });
    await ensureSchemaForCorrections(state.queryRun, op);
    op.check();
    renderProposal();
    renderTransferReceipt(state.queryRun.transfer_receipt);
    state.preflight = null;
    state.preflightReviewed = true;
    state.pendingForceSemantic = false;
    $("#ai-preflight").hidden = true;
    showFlash(
      state.queryRun.generation_strategy === "semantic_fallback"
        ? t("query.generatedSemantic")
        : t("query.generated"),
    );
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "SQL generation failed");
    invalidatePreflight();
    if (error instanceof APIError && error.structured?.code === "stale_preflight") {
      await loadPrivacyData();
    }
  } finally {
    op.finish();
  }
}

function updateSemanticRetryAvailability() {
  const button = $("#semantic-retry-button");
  if (!button) return;
  const allowed = Boolean(state.queryRun)
    && $("#privacy-mode-select").value === "governed_semantic";
  button.disabled = !allowed;
  button.title = allowed
    ? "Generate a new proposal using semantic interpretation over the same governed context."
    : "Select Governed semantics in the privacy options to allow a second provider call.";
}

async function retryProposalSemantically() {
  const op = requests.begin("retryProposalSemantically", "source");
  if (!state.queryRun || !requireSource()) return;
  const button = $("#semantic-retry-button");
  await requestAITransferPreflight(true, button);
  if (!op.current()) return;
  updateSemanticRetryAvailability();
}

function appendMeta(container, label, values) {
  const line = node("div", "meta-line");
  line.append(node("span", "", label));
  const printable = Array.isArray(values) ? (values.length ? values.join(", ") : "—") : values || "—";
  line.append(node("strong", "", printable));
  container.append(line);
}

const intentKindLabels = {
  table_preview: "table preview",
  record_list: "record list",
  record_lookup: "record lookup",
  aggregation: "aggregation",
  comparison: "comparison",
  trend: "trend",
  data_query: "data query",
};

const intentRoleLabels = {
  primary_table: "primary table",
  related_table: "related table",
  selected_column: "selected column",
  filter_column: "filter column",
  grouping_column: "grouping column",
  ordering_column: "ordering column",
};

const generationStrategyLabels = {
  deterministic: "deterministic",
  semantic_fallback: "governed semantic fallback",
  semantic_user_retry: "user-requested semantics",
};

const privacyModeLabels = {
  maximum_privacy: "maximum privacy",
  governed_semantic: "governed semantics",
};

async function ensureSchemaForCorrections(run, op) {
  if (state.schema?.catalog_version === run.context.catalog_version) return;
  try {
    state.schema = await op.api(sourcePath("/schema"));
  } catch (error) {
    if (!op.current()) throw error;
    // The retrieved generation context still provides safe correction candidates.
  }
}

function intentCorrectionCandidates(run, entity) {
  const tableRole = entity.role === "primary_table" || entity.role === "related_table";
  const catalogObjects = state.schema?.catalog_version === run.context.catalog_version
    ? state.schema.objects
    : run.context.objects;
  const references = tableRole
    ? catalogObjects.map((item) => item.reference)
    : catalogObjects.flatMap((item) =>
        item.columns.map((column) => `${item.reference}.${column.name}`),
      );
  return Array.from(new Set([entity.object_ref, ...entity.alternatives, ...references].filter(Boolean))).sort();
}

function applyIntentMemoryResult(result) {
  if (!result) return;
  if (result.requires_regeneration) {
    $("#request-state").textContent = result.query_request_state;
    $("#explain-button").disabled = true;
    $("#approve-button").disabled = true;
    $("#execute-button").disabled = true;
    $("#action-hint").textContent = "Semantic memory was corrected and the previous proposal was invalidated. Generate a new proposal.";
  }
}

async function saveIntentCorrection(entity, termInput, referenceSelect, reasonInput, button) {
  const op = requests.begin("saveIntentCorrection", "query");
  if (!state.queryRun) return;
  op.busy(button, "Saving…");
  try {
    const result = await op.api(
      sourcePath(`/query-requests/${encodeURIComponent(state.queryRun.request_id)}/intent-corrections`),
      {
        method: "POST",
        body: JSON.stringify({
          term: termInput.value.trim(),
          role: entity.role,
          corrected_object_ref: referenceSelect.value,
          previous_object_ref: entity.object_ref,
          reason: reasonInput.value.trim() || null,
        }),
      },
    );
    button.dataset.idleLabel = result.memory_action === "updated" ? "Memory updated" : "Memory created";
    applyIntentMemoryResult(result);
    showFlash(
      result.requires_regeneration
        ? "Correction remembered. Generate a new proposal to apply it to SQL."
        : "Interpretation confirmed and saved to semantic memory.",
    );
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Could not correct semantic memory");
  } finally {
    op.finish();
  }
}

async function saveFreeTextIntentCorrection(event) {
  const op = requests.begin("saveFreeTextIntentCorrection", "query");
  event.preventDefault();
  if (!state.queryRun) return;
  const button = event.submitter || $("#interpret-correction");
  const resultView = $("#intent-correction-result");
  op.busy(button, "Interpreting…");
  try {
    const run = await op.api(
      sourcePath(`/query-requests/${encodeURIComponent(state.queryRun.request_id)}/intent-corrections/from-text`),
      {
        method: "POST",
        body: JSON.stringify({
          provider_id: $("#provider-select").value.trim(),
          correction_text: $("#intent-correction-text").value.trim(),
          correction_classification: $("#classification-select").value,
          current_entities: state.queryRun.interpretation.entities.map((entity) => ({
            term: entity.term,
            role: entity.role,
            object_ref: entity.object_ref,
          })),
        }),
      },
    );
    const interpretation = run.interpretation;
    resultView.replaceChildren();
    resultView.hidden = false;
    if (interpretation.needs_clarification) {
      resultView.dataset.state = "clarification";
      resultView.append(node("strong", "", "Memory was not changed: clarification is required."));
      resultView.append(node("p", "", interpretation.reason));
      interpretation.ambiguities.forEach((value) => {
        resultView.append(node("p", "", `Clarify: ${value}`));
      });
      if (interpretation.alternatives.length) {
        resultView.append(node("p", "", `Possible objects: ${interpretation.alternatives.join(", ")}.`));
      }
      showFlash("The correction is ambiguous; semantic memory was not changed.", "error");
      return;
    }

    const memory = run.memory_correction;
    applyIntentMemoryResult(memory);
    resultView.dataset.state = "saved";
    resultView.append(node("strong", "", memory.memory_action === "updated" ? "Memory updated" : "New memory created"));
    resultView.append(node(
      "p",
      "",
      `“${interpretation.term_to_remember}” → ${interpretation.corrected_object_ref} · confidence ${Math.round(interpretation.confidence * 100)}%.`,
    ));
    resultView.append(node("p", "", interpretation.reason));
    showFlash(
      memory.requires_regeneration
        ? "Correction interpreted and remembered. Generate a new proposal to apply it to SQL."
        : "Correction interpreted and saved to semantic memory.",
    );
  } catch (error) {
    if (!op.current()) return;
    resultView.hidden = true;
    handleError(error, "Could not interpret the correction");
  } finally {
    op.finish();
  }
}

function renderInterpretation(run) {
  const interpretation = run.interpretation;
  const unresolved = interpretation.entities.some((entity) => !entity.object_ref);
  const badge = $("#intent-badge");
  badge.textContent = unresolved ? "clarification required" : "interpreted";
  badge.dataset.state = unresolved ? "error" : "ok";
  $("#intent-summary").textContent = interpretation.summary;
  $("#intent-correction-text").value = "";
  $("#intent-correction-result").hidden = true;

  const meta = $("#intent-meta");
  meta.replaceChildren();
  appendMeta(meta, "Request type", intentKindLabels[interpretation.kind] || interpretation.kind);
  appendMeta(meta, "Requested limit", interpretation.requested_row_limit ?? "not specified");

  const entities = $("#intent-entities");
  entities.replaceChildren();
  interpretation.entities.forEach((entity) => {
    const row = node("div", "intent-entity");
    const term = node("div");
    term.append(node("strong", "", `“${entity.term}”`));
    term.append(node("span", "", ` · ${intentRoleLabels[entity.role] || entity.role}`));

    const resolution = node("div");
    resolution.append(node("code", "", entity.object_ref || "unresolved"));
    resolution.append(node("span", "", ` · confidence ${Math.round(entity.confidence * 100)}%`));

    const explanation = node("div");
    explanation.append(node("span", "", entity.reason));
    if (entity.alternatives.length) {
      explanation.append(node("span", "", ` Alternatives: ${entity.alternatives.join(", ")}.`));
    }
    const correction = node("details", "intent-correction");
    correction.append(node("summary", "", "Correct or confirm this mapping"));
    const correctionFields = node("div", "intent-correction-fields");
    const termInput = node("input");
    termInput.type = "text";
    termInput.maxLength = 300;
    termInput.value = entity.term;
    termInput.setAttribute("aria-label", "Term to remember");
    const referenceSelect = node("select");
    referenceSelect.setAttribute("aria-label", "Correct object");
    intentCorrectionCandidates(run, entity).forEach((reference) => {
      const option = node("option", "", reference);
      option.value = reference;
      option.selected = reference === entity.object_ref;
      referenceSelect.append(option);
    });
    const reasonInput = node("input");
    reasonInput.type = "text";
    reasonInput.maxLength = 8000;
    reasonInput.placeholder = "Reason for the correction (optional)";
    reasonInput.setAttribute("aria-label", "Reason for the correction");
    const saveButton = node("button", "button button-secondary", "Confirm and remember");
    saveButton.type = "button";
    saveButton.addEventListener("click", () => {
      saveIntentCorrection(entity, termInput, referenceSelect, reasonInput, saveButton);
    });
    correctionFields.append(termInput, referenceSelect, reasonInput, saveButton);
    correction.append(correctionFields);
    explanation.append(correction);
    row.append(term, resolution, explanation);
    entities.append(row);
  });

  const ambiguities = $("#intent-ambiguities");
  ambiguities.replaceChildren();
  const values = run.proposal.ambiguities || [];
  ambiguities.hidden = values.length === 0;
  values.forEach((value) => {
    ambiguities.append(node("div", "validation-item blocking", `Ambiguity · ${value}`));
  });
}

function renderQueryParameters(parameters) {
  const section = $("#parameter-section");
  const fields = $("#parameter-fields");
  fields.replaceChildren();
  section.hidden = !parameters.length;
  parameters.forEach((parameter) => {
    const wrapper = node("div", "parameter-field");
    const label = node("label", "", parameter.name);
    label.htmlFor = `query-parameter-${parameter.name}`;
    label.append(node("span", "field-help", ` · ${parameter.value_type}${parameter.nullable ? " · nullable" : ""}`));
    let input;
    if (parameter.value_type === "boolean") {
      input = node("select");
      [["true", "True"], ["false", "False"]].forEach(([value, text]) => {
        const option = node("option", "", text);
        option.value = value;
        input.append(option);
      });
    } else {
      input = node("input");
      input.type = {
        integer: "number",
        number: "number",
        date: "date",
        datetime: "datetime-local",
      }[parameter.value_type] || "text";
      if (parameter.value_type === "integer") input.step = "1";
      if (parameter.value_type === "number") input.step = "any";
      if (parameter.value_type === "uuid") {
        input.placeholder = "00000000-0000-0000-0000-000000000000";
      }
      input.maxLength = parameter.value_type === "string" ? 10000 : 200;
    }
    input.id = `query-parameter-${parameter.name}`;
    input.dataset.parameterName = parameter.name;
    input.dataset.parameterType = parameter.value_type;
    input.required = !parameter.nullable;
    wrapper.append(label, input);
    if (parameter.nullable) {
      const nullableLabel = node("label", "parameter-null");
      const nullableInput = node("input");
      nullableInput.type = "checkbox";
      nullableInput.dataset.nullParameter = parameter.name;
      nullableInput.addEventListener("change", () => {
        input.disabled = nullableInput.checked;
      });
      nullableLabel.append(nullableInput, document.createTextNode(" Use NULL"));
      wrapper.append(nullableLabel);
    }
    fields.append(wrapper);
  });
}

function invalidateQueryBindings() {
  requests.invalidate("query");
  $("#explain-section").hidden = true;
  $("#approve-button").disabled = true;
  $("#execute-button").disabled = true;
}

function lockQueryParameters(locked) {
  $$("#parameter-fields input, #parameter-fields select").forEach((input) => {
    const nullable = input.dataset.parameterName
      ? $(`[data-null-parameter="${input.dataset.parameterName}"]`) : null;
    input.disabled = locked || Boolean(nullable?.checked);
  });
}

function queryParameterBindings() {
  if (!state.queryRun) return {};
  const bindings = {};
  (state.queryRun.proposal.parameters || []).forEach((parameter) => {
    const input = $(`#query-parameter-${parameter.name}`);
    const nullInput = document.querySelector(`[data-null-parameter="${parameter.name}"]`);
    if (nullInput?.checked) {
      bindings[parameter.name] = null;
      return;
    }
    const value = input.value;
    if (!value && parameter.value_type !== "boolean") {
      throw new Error(`Enter a value for parameter ${parameter.name}.`);
    }
    if (parameter.value_type === "integer") {
      if (!/^-?\d+$/.test(value)) throw new Error(`${parameter.name} must be an integer.`);
      bindings[parameter.name] = Number.parseInt(value, 10);
    } else if (parameter.value_type === "number") {
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) throw new Error(`${parameter.name} must be a finite number.`);
      bindings[parameter.name] = parsed;
    } else if (parameter.value_type === "boolean") {
      bindings[parameter.name] = value === "true";
    } else {
      bindings[parameter.name] = value;
    }
  });
  return bindings;
}

function renderProposal() {
  const run = state.queryRun;
  if (!run) return;
  $("#query-workflow").hidden = false;
  $("#explain-section").hidden = true;
  $("#result-section").hidden = true;
  $("#request-state").textContent = run.state;
  $("#sql-output code").textContent = run.validation.normalized_sql || run.proposal.sql;
  renderInterpretation(run);
  const meta = $("#proposal-meta");
  meta.replaceChildren();
  appendMeta(meta, "Tables", run.validation.referenced_tables);
  appendMeta(meta, "Columns", run.validation.referenced_columns);
  appendMeta(meta, "Assumptions", run.proposal.assumptions);
  appendMeta(meta, "Parameters", (run.proposal.parameters || []).map((item) => `${item.name}:${item.value_type}`));
  appendMeta(meta, "Output lineage", run.validation.output_lineage_complete ? "complete" : "conservative");
  appendMeta(meta, "Privacy", privacyModeLabels[run.privacy_mode] || run.privacy_mode);
  appendMeta(
    meta,
    "Strategy",
    `${generationStrategyLabels[run.generation_strategy] || run.generation_strategy} · ${run.generation_attempt_count} attempt${run.generation_attempt_count === 1 ? "" : "s"}`,
  );
  appendMeta(meta, "Model", `${run.provider_id} / ${run.model_id}`);
  renderQueryParameters(run.proposal.parameters || []);

  const validation = $("#validation-result");
  validation.replaceChildren();
  validation.className = "validation-list";
  const issues = run.validation.issues || [];
  const blocking = issues.some((issue) => issue.blocking);
  const badge = $("#validation-badge");
  badge.textContent = blocking ? "blocked" : run.validation_status;
  badge.dataset.state = blocking ? "error" : "ok";
  if (!issues.length) {
    validation.append(node("div", "validation-ok", "AST and lineage are consistent with the governed context."));
  } else {
    issues.forEach((issue) => {
      validation.append(node("div", `validation-item${issue.blocking ? " blocking" : ""}`, `${issue.code} · ${issue.message}`));
    });
  }
  $("#explain-button").disabled = !run.ready_for_preview || blocking;
  $("#approve-button").disabled = true;
  $("#execute-button").disabled = true;
  updateSemanticRetryAvailability();
  $("#action-hint").textContent = blocking
    ? "Validation blocked the proposal: correct it or generate a new one."
    : "Request the database plan to estimate cost and rows.";
  renderOnboarding();
  setWorkflowStage("proposal");
}

async function explainQuery() {
  const op = requests.begin("explainQuery", "query");
  if (!state.queryRun) return;
  const queryRun = state.queryRun;
  const button = $("#explain-button");
  op.busy(button, "Explain…");
  try {
    const result = await op.api(sourcePath(`/query-requests/${encodeURIComponent(state.queryRun.request_id)}/explain`), {
      method: "POST",
      body: JSON.stringify({parameters: queryParameterBindings()}),
    });
    if (state.queryRun !== queryRun) return;
    queryRun.explain_revision = result.revision;
    $("#estimated-cost").textContent = result.estimated_total_cost ?? "not provided";
    $("#estimated-rows").textContent = result.estimated_rows ?? "not provided";
    $("#explain-time").textContent = `${result.elapsed_ms} ms`;
    $("#plan-output code").textContent = JSON.stringify(result.plan, null, 2);
    $("#explain-section").hidden = false;
    $("#approve-button").disabled = false;
    $("#action-hint").textContent = "Review the plan and estimates, then approve the query explicitly.";
    setWorkflowStage("explain");
    showFlash(t("query.explainComplete"));
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "EXPLAIN failed");
  } finally {
    op.finish();
  }
}

async function approveQuery() {
  const op = requests.begin("approveQuery", "query");
  if (!state.queryRun) return;
  const queryRun = state.queryRun;
  const button = $("#approve-button");
  op.busy(button, "Approving…");
  try {
    const result = await op.api(sourcePath(`/query-requests/${encodeURIComponent(state.queryRun.request_id)}/approval`), {
      method: "POST",
      body: JSON.stringify({
        parameters: queryParameterBindings(),
        expected_explain_revision: queryRun.explain_revision ?? 0,
      }),
    });
    if (state.queryRun !== queryRun) return;
    $("#request-state").textContent = result.state;
    lockQueryParameters(true);
    $("#execute-button").disabled = false;
    $("#explain-button").disabled = true;
    $("#action-hint").textContent = "Ticket approved. Execution will use the same validated query in read-only mode.";
    setWorkflowStage("approval");
    showFlash(t("query.approved"));
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Approval failed");
  } finally {
    op.finish();
  }
}

function printableCell(value) {
  if (value === null) return "NULL";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function renderResultTable(result) {
  const wrapper = $("#result-table");
  wrapper.replaceChildren();
  const table = node("table", "result-table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  result.columns.forEach((column) => headRow.append(node("th", "", column)));
  head.append(headRow);
  table.append(head);
  const body = document.createElement("tbody");
  result.rows.forEach((row) => {
    const tableRow = document.createElement("tr");
    result.columns.forEach((column) => tableRow.append(node("td", "", printableCell(row[column]))));
    body.append(tableRow);
  });
  table.append(body);
  wrapper.append(table);
}

async function executeQuery() {
  const op = requests.begin("executeQuery", "query");
  if (!state.queryRun) return;
  const queryRun = state.queryRun;
  const requestPath = sourcePath(`/query-requests/${encodeURIComponent(queryRun.request_id)}`);
  let canExecute = true;
  const button = $("#execute-button");
  op.busy(button, "Executing…");
  try {
    const run = await op.api(`${requestPath}/executions`, {
      method: "POST",
      body: JSON.stringify({parameters: queryParameterBindings()}),
    });
    if (state.queryRun !== queryRun) return;
    canExecute = false;
    $("#request-state").textContent = run.query_request.state;
    $("#result-summary").textContent = run.answer.summary;
    $("#privacy-badge").textContent = `${run.privacy.processing_mode} · ${run.privacy.maximum_classification}`;
    renderResultTable(run.result);
    $("#provenance-output code").textContent = JSON.stringify({privacy: run.privacy, provenance: run.provenance}, null, 2);
    $("#result-section").hidden = false;
    $("#approve-button").disabled = true;
    $("#explain-button").disabled = true;
    $("#action-hint").textContent = `Execution completed: ${run.result.row_count} rows${run.result.truncated ? ", result truncated" : ""}.`;
    setWorkflowStage("execution");
    showFlash(t("query.executed"));
  } catch (error) {
    if (!op.current()) return;
    if (state.queryRun !== queryRun) return;
    if (error instanceof APIError && error.status === 409) {
      canExecute = false;
      try {
        const current = await op.api(requestPath);
        if (state.queryRun !== queryRun) return;
        $("#request-state").textContent = current.state;
        canExecute = current.state === "approved";
        if (current.state === "ready_for_preview") {
          lockQueryParameters(false);
          queryRun.explain_revision = current.explain_revision;
          $("#explain-button").disabled = false;
          $("#approve-button").disabled = false;
          $("#action-hint").textContent = "Execution policy changed. Review the plan and approve again.";
          setWorkflowStage("explain");
        }
      } catch (refreshError) {
    if (!op.current()) return;
        if (state.queryRun !== queryRun) return;
        handleError(refreshError, "Could not refresh query state");
      }
    }
    handleError(error, "Execution failed");
  } finally {
    if (op.current() && state.queryRun === queryRun) {
      op.finish();
      button.disabled = !canExecute;
    }
  }
}

function renderAdminItems(container, items, emptyText, renderItem) {
  container.replaceChildren();
  container.classList.toggle("empty-state", items.length === 0);
  if (items.length === 0) {
    container.append(node("p", "", emptyText));
    return;
  }
  items.forEach((item) => container.append(renderItem(item)));
}

const federatedRoleGuidance = {
  viewer: "Viewer: read-only access to governed metadata; cannot use Query Studio.",
  analyst: "Analyst: can generate, review, approve, and execute governed queries, and submit feedback.",
  data_steward: "Data steward: analyst access plus data-source, semantic, audit, and governed-review responsibilities.",
  admin: "Tenant admin: manages tenant identities, security, and FinOps. This is not platform-wide bootstrap authority.",
};

function renderFederatedRoleGuidance() {
  $("#federated-role-help").textContent = federatedRoleGuidance[$("#federated-role").value];
}

async function loadAdminData() {
  const op = requests.begin("loadAdminData", "source");
  if (!state.tenantId) {
    renderAdminItems($("#principal-list"), [], "Select a tenant.", () => node("div"));
    renderAdminItems($("#job-list"), [], "Select a tenant.", () => node("div"));
    return;
  }
  const tenantPath = `/v1/tenants/${encodeURIComponent(state.tenantId)}`;
  try {
    const principals = await op.api(`${tenantPath}/security/principals`);
    renderAdminItems($("#principal-list"), principals, "No identities provisioned.", (access) => {
      const item = node("div", "admin-item");
      item.append(node("strong", "", access.principal.display_name));
      const roles = [
        ...access.tenant_roles,
        ...access.data_source_roles.map((assignment) => `${assignment.role}@${assignment.data_source_id}`),
      ];
      item.append(node("span", "", `${access.principal.subject} · ${roles.join(", ") || "no roles"}`));
      item.append(node("span", "", `${access.credentials.length} API credentials`));
      return item;
    });
  } catch (error) {
    if (!op.current()) return;
    renderAdminItems($("#principal-list"), [], error.message, () => node("div"));
  }
  try {
    const jobs = await op.api(`${tenantPath}/background-jobs?limit=50`);
    renderAdminItems($("#job-list"), jobs, "No queued jobs.", (job) => {
      const item = node("div", "admin-item");
      item.append(node("strong", "", `${job.job_type} · ${job.status}`));
      item.append(node("span", "", `${job.id} · attempt ${job.attempt_count}/${job.max_attempts}`));
      if (job.last_error_code) item.append(node("span", "", `Last error: ${job.last_error_code}`));
      if (job.status === "queued") {
        const cancel = node("button", "button button-quiet", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", () => cancelJob(job.id));
        item.append(cancel);
      }
      return item;
    });
  } catch (error) {
    if (!op.current()) return;
    renderAdminItems($("#job-list"), [], error.message, () => node("div"));
  }
}

async function createFederatedPrincipal(event) {
  const op = requests.begin("createFederatedPrincipal", "source");
  event.preventDefault();
  if (!state.tenantId) return showFlash("Select a tenant first.", "error");
  const sourceScoped = $("#federated-source-scope").checked;
  if (sourceScoped && !state.sourceId) return showFlash("Select the data source to authorize.", "error");
  const button = event.submitter;
  op.busy(button, "Provisioning…");
  try {
    await op.api(`/v1/tenants/${encodeURIComponent(state.tenantId)}/security/federated-principals`, {
      method: "POST",
      body: JSON.stringify({
        subject: $("#federated-subject").value.trim(),
        display_name: $("#federated-display-name").value.trim(),
        role: $("#federated-role").value,
        data_source_ids: sourceScoped ? [state.sourceId] : [],
      }),
    });
    event.target.reset();
    renderFederatedRoleGuidance();
    await loadAdminData();
    op.check();
    showFlash("OIDC identity provisioned without creating an API key.");
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Identity provisioning failed");
  } finally {
    op.finish();
  }
}

async function saveProviderPolicy(event) {
  const op = requests.begin("saveProviderPolicy", "source");
  event.preventDefault();
  if (!state.tenantId) return showFlash("Select a tenant first.", "error");
  const sourceScoped = $("#policy-source-scope").checked;
  if (sourceScoped && !state.sourceId) return showFlash("Select the policy data source.", "error");
  const providerId = $("#policy-provider").value.trim();
  const base = `/v1/tenants/${encodeURIComponent(state.tenantId)}`;
  const scope = sourceScoped
    ? `/data-sources/${encodeURIComponent(state.sourceId)}`
    : "";
  const button = event.submitter;
  op.busy(button, "Saving…");
  try {
    const purposes = $$('input[name="policy-purpose"]:checked').map((input) => input.value);
    if (!purposes.length) throw new Error("Select at least one authorized purpose.");
    await op.api(`${base}${scope}/provider-egress-policies/${encodeURIComponent(providerId)}`, {
      method: "PUT",
      body: JSON.stringify({
        allowed: $("#policy-allowed").checked,
        maximum_classification: $("#policy-classification").value,
        allowed_purposes: purposes,
        data_residency: $("#policy-residency").value.trim(),
        retention_mode: $("#policy-retention").value,
        acknowledged: $("#policy-acknowledged").checked,
      }),
    });
    $("#policy-acknowledged").checked = false;
    await loadPrivacyData();
    op.check();
    showFlash("Provider authorization updated and bound to the declared deployment.");
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Could not save the policy");
  } finally {
    op.finish();
  }
}

async function testSelectedConnection() {
  const op = requests.begin("testSelectedConnection", "source");
  if (!requireSource()) return;
  const button = $("#test-connection");
  op.busy(button, "Test…");
  try {
    const result = await op.api(sourcePath("/connection-tests"), {method: "POST"});
    const box = $("#admin-operation-result");
    box.textContent = `Connection verified: ${result.object_count} objects, ${result.relationship_count} relationships, capabilities ${result.capabilities.join(", ")}.`;
    box.hidden = false;
    showFlash("Connection test completed and audited.");
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Connection test failed");
  } finally {
    op.finish();
  }
}

async function enqueueInferenceJob() {
  const op = requests.begin("enqueueInferenceJob", "source");
  if (!requireSource()) return;
  const providerId = $("#inference-provider").value.trim();
  if (!providerId) return showFlash("Select the provider for schema description inference.", "error");
  const button = $("#enqueue-inference");
  op.busy(button, "Queuing…");
  try {
    await op.api(sourcePath("/semantics/inference-jobs"), {
      method: "POST",
      body: JSON.stringify({provider_id: providerId, max_attempts: 3}),
    });
    await loadAdminData();
    op.check();
    showFlash("Semantic inference queued in the durable worker.");
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Could not queue semantic inference");
  } finally {
    op.finish();
  }
}

async function cancelJob(jobId) {
  const op = requests.begin("cancelJob", "source");
  try {
    await op.api(`/v1/tenants/${encodeURIComponent(state.tenantId)}/background-jobs/${encodeURIComponent(jobId)}`, {method: "DELETE"});
    await loadAdminData();
    op.check();
    showFlash("Job cancelled.");
  } catch (error) {
    if (!op.current()) return;
    handleError(error, "Could not cancel the job");
  }
}

function initializeExamples() {
  $("#manual-input").value = JSON.stringify({
    objects: [{
      schema_name: "public",
      name: "orders",
      kind: "table",
      columns: [
        {name: "id", physical_type: "bigint", ordinal: 1, nullable: false, is_primary_key: true, classification: "internal"},
        {name: "created_at", physical_type: "timestamp", ordinal: 2, nullable: false, classification: "internal"},
      ],
    }],
    relationships: [],
  }, null, 2);
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => {
    switchPanel(button.dataset.panel);
    if (button.dataset.panel === "privacy" || button.dataset.panel === "query") loadPrivacyData();
    if (button.dataset.panel === "admin") loadAdminData();
  }));
  $("#connection-form").addEventListener("submit", connectConsole);
  $("#disconnect-button").addEventListener("click", disconnectConsole);
  $("#toggle-token").addEventListener("click", () => {
    const input = $("#api-token");
    input.type = input.type === "password" ? "text" : "password";
    $("#toggle-token").textContent = input.type === "password" ? "Show" : "Hide";
  });
  $("#tenant-form").addEventListener("submit", createTenant);
  $("#tenant-select").addEventListener("change", (event) => {
    if (event.target.value) selectTenant(event.target.value);
  });
  $("#use-tenant-id").addEventListener("click", async () => {
    const id = $("#tenant-id-manual").value.trim();
    if (!id) return;
    await selectTenant(id);
    if (state.tenantId === id) showFlash(`Tenant ID ${id} selected.`);
  });
  $("#source-form").addEventListener("submit", createSource);
  $("#source-type").addEventListener("change", renderSourceModeGuidance);
  $("#apply-source-defaults").addEventListener("click", applyRecommendedSourceCapabilities);
  $("#refresh-sources").addEventListener("click", loadSources);
  $$("[data-import-tab]").forEach((tab) => tab.addEventListener("click", () => {
    if (!tab.disabled) setImportTab(tab.dataset.importTab);
  }));
  $("#run-introspection").addEventListener("click", runIntrospection);
  $("#ddl-form").addEventListener("submit", importDDL);
  $("#manual-form").addEventListener("submit", importManual);
  $("#refresh-schema").addEventListener("click", loadSchema);
  $("#schema-search").addEventListener("input", renderSchema);
  $("#query-form").addEventListener("submit", generateProposal);
  $("#parameter-fields").addEventListener("input", invalidateQueryBindings);
  ["#provider-select", "#question-input", "#classification-select", "#privacy-mode-select"].forEach((selector) => {
    $(selector).addEventListener("input", () => {
      invalidatePreflight();
      renderQueryPrivacyStatus();
      updateSemanticRetryAvailability();
    });
  });
  $("#confirm-ai-transfer").addEventListener("click", confirmAITransfer);
  $("#cancel-ai-transfer").addEventListener("click", () => {
    invalidatePreflight();
    showFlash(t("privacy.cancelled"));
  });
  $("#open-privacy-setup").addEventListener("click", () => {
    switchPanel("privacy");
    loadPrivacyData();
  });
  $("#open-privacy-admin").addEventListener("click", () => {
    switchPanel("privacy");
    loadPrivacyData();
  });
  $("#refresh-privacy").addEventListener("click", loadPrivacyData);
  $("#semantic-retry-button").addEventListener("click", retryProposalSemantically);
  $("#intent-correction-form").addEventListener("submit", saveFreeTextIntentCorrection);
  $("#explain-button").addEventListener("click", explainQuery);
  $("#approve-button").addEventListener("click", approveQuery);
  $("#execute-button").addEventListener("click", executeQuery);
  $("#refresh-admin").addEventListener("click", loadAdminData);
  $("#federated-principal-form").addEventListener("submit", createFederatedPrincipal);
  $("#provider-policy-form").addEventListener("submit", saveProviderPolicy);
  $("#policy-source-scope").addEventListener("change", renderPolicyScopeSummary);
  $("#policy-provider").addEventListener("input", () => {
    const item = state.privacyProviders.find((entry) => entry.deployment.provider_id === $("#policy-provider").value.trim());
    if (item) selectPrivacyProvider(item);
  });
  $("#test-connection").addEventListener("click", testSelectedConnection);
  $("#enqueue-inference").addEventListener("click", enqueueInferenceJob);
  $("#federated-role").addEventListener("change", renderFederatedRoleGuidance);
  $("#copy-sql").addEventListener("click", async () => {
    const sql = $("#sql-output code").textContent;
    if (!navigator.clipboard || !sql) {
      showFlash("Automatic copy is not available in this context.", "error");
      return;
    }
    try {
      await navigator.clipboard.writeText(sql);
      showFlash("SQL copied to the clipboard.");
    } catch (error) {
      handleError(error, "Copy failed");
    }
  });
  $("#help-button").addEventListener("click", () => openHelp());
  $("#close-help").addEventListener("click", closeHelp);
  $("#help-backdrop").addEventListener("click", closeHelp);
  $("#help-search").addEventListener("input", (event) => renderHelpTopics(event.target.value));
  $$(".help-trigger").forEach((button) => {
    button.addEventListener("click", () => openHelp(button.dataset.help));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#help-drawer").hidden) closeHelp();
  });
}

globalThis.SQLVerityI18n.apply();
initializeExamples();
bindEvents();
renderSourceModeGuidance();
renderFederatedRoleGuidance();
renderPolicyScopeSummary();
renderConnection();
renderCapabilities();
renderTenantOptions();
renderSources();
updateAcquisitionOptions();
resetSchema();
resetQueryWorkflow();
renderPrivacyProviders();
renderQueryPrivacyStatus();
updateContextHeader();
renderOnboarding();
initializeOIDC();
