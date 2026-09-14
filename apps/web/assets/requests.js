"use strict";

// Context generations also reject completed responses whose abort arrived too late.
globalThis.SQLVerityRequests = class {
  constructor(api) {
    this.api = api;
    this.generations = [0, 0, 0, 0];
    this.latest = new Map();
    this.levels = {session: 0, tenant: 1, source: 2, query: 3};
  }

  invalidate(scope) {
    const level = this.levels[scope];
    for (let index = level; index < this.generations.length; index++) this.generations[index]++;
    for (const operation of this.latest.values()) {
      if (operation.level >= level) operation.cancel();
    }
  }

  begin(key, scope = "source") {
    this.latest.get(key)?.cancel();
    const controller = new AbortController();
    const level = this.levels[scope];
    const generations = this.generations.slice(0, level + 1);
    let restoreButton = null;
    const operation = {
      level,
      current: () => this.latest.get(key) === operation && !controller.signal.aborted
        && generations.every((value, index) => value === this.generations[index]),
      check: () => {
        if (!operation.current()) {
          const error = new Error("Request belongs to an obsolete console context");
          error.name = "AbortError";
          throw error;
        }
      },
      wait: async (promise) => {
        try {
          const result = await promise;
          operation.check();
          return result;
        } catch (error) {
          operation.check();
          throw error;
        }
      },
      api: (path, options = {}) => {
        operation.check();
        return operation.wait(this.api(path, {...options, signal: controller.signal}));
      },
      busy: (button, label) => {
        if (!button) return;
        const text = button.textContent;
        const disabled = button.disabled;
        button.textContent = label;
        button.disabled = true;
        restoreButton = () => { button.textContent = text; button.disabled = disabled; };
      },
      finish: () => { restoreButton?.(); restoreButton = null; },
      cancel: () => { controller.abort(); operation.finish(); },
    };
    this.latest.set(key, operation);
    return operation;
  }
};
