const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function consoleFixture() {
  const elements = new Map();
  const pending = [];
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, {
      value: "", textContent: "Ready", disabled: false, hidden: true, dataset: {},
      classList: {toggle() {}, add() {}, remove() {}}, replaceChildren() {}, append() {}, reset() {},
    });
    return elements.get(selector);
  };
  const context = vm.createContext({
    AbortController, Headers, setTimeout, clearTimeout,
    document: {querySelector: element, querySelectorAll: () => []},
    fetch: (url, options) => new Promise((resolve, reject) => pending.push({
      url, options, reject,
      respond(payload, status = 200) {
        // Deliberately ignore AbortSignal: generation checks must still reject this response.
        resolve({ok: status < 400, status, headers: new Headers({"content-type": "application/json"}),
          json: async () => payload});
      },
    })),
  });
  for (const file of ["requests.js", "app.js"]) {
    const source = fs.readFileSync(path.resolve(__dirname, "../../apps/web/assets", file), "utf8");
    vm.runInContext(source.split("globalThis.SQLVerityI18n.apply();")[0], context);
  }
  vm.runInContext(`
    globalThis.SQLVerityI18n = {t: key => key};
    globalThis.flashes = [];
    showFlash = (message) => flashes.push(message);
    for (const name of ["renderTenantOptions", "updateContextHeader", "resetSchema",
      "updateAcquisitionOptions", "renderSources", "renderConnection", "renderCapabilities",
      "renderPrivacyProviders", "renderQueryPrivacyStatus", "renderOnboarding", "renderSchema",
      "renderPreflight", "renderProposal", "renderTransferReceipt", "renderPermissions",
      "renderAdminItems", "setWorkflowStage", "renderResultTable"]) globalThis[name] = () => {};
    state.token = "synthetic-key"; state.authMode = "api_key";
    state.tenantId = "A"; state.sourceId = "a";
    state.sources = [{id: "a", name: "A"}, {id: "b", name: "B"}];
    queryParameterBindings = () => ({});
  `, context);
  return {
    run: (code) => vm.runInContext(code, context),
    state: () => JSON.parse(vm.runInContext("JSON.stringify(state)", context)),
    pending, element,
  };
}

const discovery = (id, source) => ({platform_permissions: [], tenants: [{
  id, name: id, permissions: [], data_sources: [{source: {id: source, name: source}, permissions: ["read"]}],
}]});

test("late tenant discovery cannot overwrite a newer tenant or its source list", async () => {
  const f = consoleFixture();
  const a = f.run('selectTenant("A")');
  const b = f.run('selectTenant("B")');
  f.pending[1].respond(discovery("B", "b"));
  await b;
  f.pending[0].respond(discovery("A", "a"));
  await a;
  assert.equal(f.state().tenantId, "B");
  assert.deepEqual(f.state().sources.map(x => x.id), ["b"]);
  assert.equal(f.pending[0].options.signal.aborted, true);
});

test("an old failed refresh cannot clear a newer schema or restore its busy button", async () => {
  const f = consoleFixture();
  const old = f.run("loadSchema()");
  const recent = f.run("loadSchema()");
  f.pending[0].respond({detail: "Old error"}, 500);
  await old;
  assert.equal(f.element("#refresh-schema").disabled, true);
  f.pending[1].respond({catalog_version: 2, objects: []});
  await recent;
  assert.equal(f.state().schema.catalog_version, 2);
  assert.equal(f.element("#refresh-schema").disabled, false);
});

for (const [action, stateKey] of [["loadSchema()", "schema"], ["loadPrivacyData()", "privacyProviders"]]) {
  test(`source change rejects late ${stateKey} success and error`, async () => {
    for (const status of [200, 500]) {
      const f = consoleFixture();
      const old = f.run(action);
      f.run('selectSource("b")');
      f.pending[0].respond(status === 200 ? {stale: true} : {detail: "Old error"}, status);
      await old;
      assert.equal(f.state().sourceId, "b");
      assert.deepEqual(f.state()[stateKey], stateKey === "schema" ? null : []);
      assert.equal(f.run('flashes.some(x => String(x).includes("Old error"))'), false);
    }
  });
}

test("edited question invalidates an in-flight preflight, even if abort is ignored", async () => {
  const f = consoleFixture();
  const pending = f.run('requestAITransferPreflight(false, document.querySelector("#preflight-button"))');
  f.run("invalidatePreflight()");
  f.pending[0].respond({allowed: true, confirmation_token: "obsolete"});
  await pending;
  assert.equal(f.state().preflight, null);
  assert.equal(f.state().preflightReviewed, false);
});

test("editing bindings rejects an in-flight EXPLAIN and keeps approval disabled", async () => {
  const f = consoleFixture();
  f.run('state.queryRun = {request_id: "query", explain_revision: 1};');
  const old = f.run("explainQuery()");
  f.run("invalidateQueryBindings()");
  f.pending[0].respond({revision: 2, estimated_total_cost: 1, plan: {}});
  await old;
  assert.equal(f.state().queryRun.explain_revision, 1);
  assert.equal(f.element("#approve-button").disabled, true);
  assert.equal(f.element("#explain-section").hidden, true);
});

for (const action of ["confirmAITransfer()", "explainQuery()", "approveQuery()", "executeQuery()"]) {
  test(`logout rejects late ${action} and leaves the replacement session intact`, async () => {
    const f = consoleFixture();
    f.run('state.preflight = {confirmation_token: "synthetic"}; state.queryRun = {request_id: "old"};');
    const old = f.run(action);
    await f.run("disconnectConsole()");
    f.run('state.token = "new-session"; state.authMode = "api_key"; state.queryRun = {request_id: "new"};');
    f.pending[0].respond({state: "approved", revision: 9});
    await old;
    assert.equal(f.state().token, "new-session");
    assert.equal(f.state().queryRun.request_id, "new");
    assert.equal(f.element("#result-section").hidden, true);
    assert.equal(f.element("#execute-button").disabled, true);
  });
}

test("late OIDC startup cannot replace explicit API-key login", async () => {
  const f = consoleFixture();
  const old = f.run("initializeOIDC()");
  f.run('resetConsoleSession(); state.token = "chosen"; state.authMode = "api_key";');
  f.pending[0].respond({enabled: true, login_url: "/login"});
  await old;
  assert.equal(f.state().token, "chosen");
  assert.equal(f.state().authMode, "api_key");
  assert.equal(f.pending.length, 1);
});

test("old connection failure cannot disconnect a successful newer login", async () => {
  const f = consoleFixture();
  f.element("#api-token").value = "old";
  const old = f.run('connectConsole({preventDefault() {}, submitter: document.querySelector("#connect")})');
  f.element("#api-token").value = "new";
  const recent = f.run('connectConsole({preventDefault() {}, submitter: document.querySelector("#connect")})');
  f.pending[1].respond({platform_permissions: [], tenants: []});
  await recent;
  f.pending[0].respond({detail: "Denied old login"}, 401);
  await old;
  assert.equal(f.state().token, "new");
  assert.equal(f.state().connected, true);
});
