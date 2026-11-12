"use strict";

// Shared construction helpers for the window-lifecycle test files, split out
// with the suite that uses them (file-line cap).

function validOptions(overrides = {}) {
  return {
    electron: {},
    store: { get: () => null },
    backendUrl: "http://localhost:5476",
    port: 5476,
    mintLocalToken: async () => "",
    fetchRemoteToken: async () => ({ token: "" }),
    requestQuit: () => {},
    connectWindow: async () => {},
    // Keep construction independent of the host running the suite.
    platform: "test",
    ...overrides,
  };
}

function trayElectron(trays = []) {
  return {
    app: { name: "Kiro Crew", getVersion: () => "0.8.0", getPath: () => "/virtual/logs" },
    Tray: class {
      constructor() { this.destroyed = false; trays.push(this); }
      setToolTip() {}
      setContextMenu() {}
      on() {}
      destroy() { this.destroyed = true; }
    },
    Menu: { buildFromTemplate: (template) => ({ template }) },
    nativeImage: {
      createFromPath: () => ({ resize: () => ({ fake: "icon" }), setTemplateImage() {} }),
    },
    shell: {},
  };
}

module.exports = { validOptions, trayElectron };
