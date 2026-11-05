"use strict";

function createTrayControls({
  getGatewayProcess,
  getGatewayOwnership,
  startGateway,
  stopGatewayGracefully,
  getMainWindow,
  log,
}) {
  let operation = null;
  let inFlight = false;
  let onStateChange = () => {};

  function notifyStateChange() {
    try {
      onStateChange();
    } catch (error) {
      log(`gateway state observer threw: ${error && error.message}`);
    }
  }

  function sendStatus(message) {
    const contents = getMainWindow()?.webContents;
    if (!contents || contents.isDestroyed()) return;
    contents.send("status", message);
  }

  function controlState() {
    const gatewayOwnership = getGatewayOwnership();
    const gatewayProcess = getGatewayProcess();
    const adopted = gatewayOwnership !== "none" && gatewayOwnership !== "spawned";
    return {
      running: gatewayProcess !== null || adopted,
      owned: gatewayOwnership === "spawned" && gatewayProcess !== null,
      operation,
    };
  }

  async function runControlAction(action) {
    if (inFlight || !["start", "stop", "restart"].includes(action)) return;
    if (action !== "start" && !controlState().owned) return;
    inFlight = true;
    operation = action.charAt(0).toUpperCase() + action.slice(1);
    notifyStateChange();
    try {
      if (action !== "start") await stopGatewayGracefully();
      if (action !== "stop") await startGateway();
    } catch (error) {
      const message = (error && error.message) || String(error);
      log(`gateway ${action} from the tray failed: ${message}`);
      sendStatus(`Gateway ${action} failed: ${message}`);
    } finally {
      inFlight = false;
      operation = null;
      notifyStateChange();
    }
  }

  function observeState(listener) {
    onStateChange = typeof listener === "function" ? listener : () => {};
  }

  return Object.freeze({ controlState, runControlAction, observeState, notifyStateChange, sendStatus });
}

module.exports = { createTrayControls };
