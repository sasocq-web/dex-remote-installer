#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");

const mode = String(process.argv[2] || "status").toLowerCase();
if (!new Set(["status", "mobile", "desktop"]).has(mode)) {
  console.error("modo inválido; use status, mobile ou desktop");
  process.exit(2);
}

const stateFile = path.join(process.env.HOME || "/tmp", ".local", "share", "codex-linux-control", "browser-layout-state.json");

function readState() {
  try {
    const value = JSON.parse(fs.readFileSync(stateFile, "utf8"));
    return value && typeof value === "object" ? value : {targets:{}};
  } catch {
    return {targets:{}};
  }
}

function writeState(value) {
  fs.mkdirSync(path.dirname(stateFile), {recursive:true, mode:0o700});
  const temporary = `${stateFile}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {mode:0o600});
  fs.renameSync(temporary, stateFile);
  fs.chmodSync(stateFile, 0o600);
}

class CdpClient {
  constructor(url) {
    this.url = url;
    this.ws = null;
    this.sequence = 0;
    this.pending = new Map();
  }

  async open() {
    this.ws = new WebSocket(this.url);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("tempo limite ao conectar ao CDP")), 3000);
      this.ws.addEventListener("open", () => { clearTimeout(timer); resolve(); }, {once:true});
      this.ws.addEventListener("error", () => { clearTimeout(timer); reject(new Error("falha ao conectar ao CDP")); }, {once:true});
    });
    this.ws.addEventListener("message", event => {
      let message;
      try { message = JSON.parse(String(event.data)); } catch { return; }
      if (!message.id || !this.pending.has(message.id)) return;
      const pending = this.pending.get(message.id);
      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      if (message.error) pending.reject(new Error(message.error.message || "erro do CDP"));
      else pending.resolve(message.result || {});
    });
  }

  send(method, params = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`tempo limite no comando ${method}`));
      }, 4000);
      this.pending.set(id, {resolve, reject, timer});
      this.ws.send(JSON.stringify({id, method, params}));
    });
  }

  close() {
    try { this.ws?.close(); } catch { /* conexão efêmera de diagnóstico */ }
  }
}

async function inspectTarget(target) {
  const client = new CdpClient(target.webSocketDebuggerUrl);
  try {
    await client.open();
    const response = await client.send("Runtime.evaluate", {
      expression:"JSON.stringify({focused:document.hasFocus(),visibility:document.visibilityState,width:innerWidth,height:innerHeight,screenWidth:screen.width,screenHeight:screen.height})",
      returnByValue:true,
    });
    const value = response?.result?.value;
    const geometry = value ? JSON.parse(value) : {};
    return {target, geometry};
  } finally {
    client.close();
  }
}

async function activePageTarget() {
  const response = await fetch("http://127.0.0.1:9223/json/list", {signal:AbortSignal.timeout(4000)});
  if (!response.ok) throw new Error(`CDP respondeu HTTP ${response.status}`);
  const targets = await response.json();
  const pages = targets.filter(target => target.type === "page" && target.webSocketDebuggerUrl && !String(target.url || "").startsWith("chrome://"));
  if (!pages.length) throw new Error("nenhuma página web controlável está aberta");
  const inspected = [];
  for (const target of pages) {
    try { inspected.push(await inspectTarget(target)); } catch { /* uma aba encerrada durante a inspeção é ignorada */ }
  }
  if (!inspected.length) throw new Error("nenhuma página web respondeu ao CDP");
  inspected.sort((left, right) => {
    const score = item => (item.geometry.focused ? 100 : 0)
      + (item.geometry.visibility === "visible" ? 20 : 0)
      + (item.target.url !== "about:blank" ? 5 : 0);
    return score(right) - score(left);
  });
  return inspected[0];
}

async function main() {
  const selected = await activePageTarget();
  const saved = readState();
  if (!saved.targets || typeof saved.targets !== "object") saved.targets = {};
  const client = new CdpClient(selected.target.webSocketDebuggerUrl);
  try {
    await client.open();
    if (mode === "mobile") {
      if (!saved.targets[selected.target.id]) {
        saved.targets[selected.target.id] = {
          width:Math.max(240, Number(selected.geometry.width || 0)),
          height:Math.max(320, Number(selected.geometry.height || 0)),
          screenWidth:Math.max(240, Number(selected.geometry.screenWidth || selected.geometry.width || 0)),
          screenHeight:Math.max(320, Number(selected.geometry.screenHeight || selected.geometry.height || 0)),
        };
        writeState(saved);
      }
      await client.send("Emulation.setDeviceMetricsOverride", {
        width:390,
        height:844,
        deviceScaleFactor:2.75,
        mobile:true,
        scale:1,
        screenWidth:390,
        screenHeight:844,
        positionX:0,
        positionY:0,
        screenOrientation:{type:"portraitPrimary", angle:0},
      });
      await client.send("Emulation.setTouchEmulationEnabled", {enabled:true, maxTouchPoints:5});
    } else if (mode === "desktop") {
      const original = saved.targets[selected.target.id] || {
        width:Math.max(640, Number(selected.geometry.screenWidth || 0)),
        height:Math.max(480, Number(selected.geometry.screenHeight || 0)),
        screenWidth:Math.max(640, Number(selected.geometry.screenWidth || 0)),
        screenHeight:Math.max(480, Number(selected.geometry.screenHeight || 0)),
      };
      await client.send("Emulation.setDeviceMetricsOverride", {
        width:original.width,
        height:original.height,
        deviceScaleFactor:1,
        mobile:false,
        scale:1,
        screenWidth:original.screenWidth,
        screenHeight:original.screenHeight,
        positionX:0,
        positionY:0,
        screenOrientation:{type:original.width >= original.height ? "landscapePrimary" : "portraitPrimary", angle:0},
      });
      await client.send("Emulation.setTouchEmulationEnabled", {enabled:false});
      delete saved.targets[selected.target.id];
      writeState(saved);
    }
    const response = await client.send("Runtime.evaluate", {
      expression:"JSON.stringify({focused:document.hasFocus(),visibility:document.visibilityState,width:innerWidth,height:innerHeight,screenWidth:screen.width,screenHeight:screen.height})",
      returnByValue:true,
    });
    const geometry = JSON.parse(response?.result?.value || "{}");
    // Another Playwright CDP client may refresh screen.* from the physical
    // browser window, while the emulated page viewport remains authoritative.
    const mobile = Number(geometry.width || 0) > 0 && Number(geometry.width || 0) <= 480;
    console.log(JSON.stringify({ok:true, mobile, target_id:selected.target.id, geometry}));
  } finally {
    client.close();
  }
}

main().catch(error => {
  console.error(String(error?.message || error));
  process.exitCode = 1;
});
