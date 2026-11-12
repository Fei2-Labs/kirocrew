"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// The tray-close behaviour pins, split out of window-lifecycle.test.js for the
// file-line cap. SOURCE normalizes to LF for the same reason as that file.
const SOURCE = fs
  .readFileSync(path.join(__dirname, "..", "window-lifecycle.js"), "utf8")
  .replace(/\r\n/g, "\n");
const { createWindowLifecycle } = require("../window-lifecycle");
const { validOptions, trayElectron } = require("./window-lifecycle-test-helpers");

describe("tray icon setting", () => {
  function trayHarness(stored = {}, platform = "linux") {
    const data = { ...stored };
    const trays = [];
    const lifecycle = createWindowLifecycle(validOptions({
      electron: trayElectron(trays),
      store: { path: "/virtual/config.json", get: (key) => data[key], set: (key, value) => { data[key] = value; } },
      platform,
      env: {},
    }));
    const live = () => trays.filter((tray) => !tray.destroyed).length;
    return { lifecycle, data, trays, live };
  }

  it("creates the icon at startup unless the user hid it while the close button quits", () => {
    const shown = trayHarness();
    shown.lifecycle.syncTray();
    assert.equal(shown.live(), 1);

    const hidden = trayHarness({ closeAction: "quit", showTrayIcon: false });
    hidden.lifecycle.syncTray();
    assert.equal(hidden.trays.length, 0, "no Tray is created");
  });

  it("hides and shows the icon live, and brings it back when the close button hides the window again", () => {
    const h = trayHarness({ closeAction: "quit" });
    h.lifecycle.syncTray();
    assert.deepEqual(h.lifecycle.trayIcon.set(false), { show: false, canHide: true });
    assert.equal(h.live(), 0);
    assert.equal(h.lifecycle.getTray(), null);

    h.lifecycle.closeAction.set("background");
    assert.equal(h.live(), 1, "the way back to a hidden window returns with the setting");
    assert.deepEqual(h.lifecycle.trayIcon.get(), { show: true, canHide: false });

    h.lifecycle.closeAction.set("quit");
    assert.equal(h.live(), 0, "the stored choice applies again once the close button quits");
    assert.equal(h.lifecycle.trayIcon.set(true).show, true);
    assert.equal(h.live(), 1);
    assert.equal(h.trays.length, 3);
  });

  it("refuses to hide the icon while the close button hides the window off macOS", () => {
    const h = trayHarness({ closeAction: "background" });
    h.lifecycle.syncTray();
    assert.deepEqual(h.lifecycle.trayIcon.set(false), { show: true, canHide: false });
    assert.equal(h.live(), 1);
    assert.equal(h.data.showTrayIcon, undefined, "nothing is written");
  });

  it("re-syncs the icon before the close button hides the window", () => {
    // The close action is read from the store at close time, so a hand-edited
    // switch to hide must bring the icon back before the window disappears.
    const hide = SOURCE.slice(SOURCE.indexOf("close: hiding to tray"));
    const sync = hide.indexOf("syncTray();");
    assert.ok(sync !== -1 && sync < hide.indexOf("hideToTray(mainWindow"),
      "the close handler's hide path calls syncTray() before hideToTray()");
  });

  it("hides the icon on macOS whatever the close button does", () => {
    const h = trayHarness({ closeAction: "background" }, "darwin");
    h.lifecycle.syncTray();
    assert.deepEqual(h.lifecycle.trayIcon.set(false), { show: false, canHide: true });
    assert.equal(h.live(), 0);
  });
});
