#!/usr/bin/env node

const remoteX = Number(process.argv[2]);
const remoteY = Number(process.argv[3]);
const activeWindowTitle = String(process.argv[4] || "").trim();
if (!Number.isFinite(remoteX) || !Number.isFinite(remoteY)) {
  console.error("coordenadas remotas inválidas");
  process.exit(2);
}

class CdpClient {
  constructor(url) {
    this.url = url;
    this.sequence = 0;
    this.pending = new Map();
  }

  async open() {
    this.socket = new WebSocket(this.url);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("tempo limite ao conectar ao CDP")), 2500);
      this.socket.addEventListener("open", () => { clearTimeout(timer); resolve(); }, {once:true});
      this.socket.addEventListener("error", () => { clearTimeout(timer); reject(new Error("falha ao conectar ao CDP")); }, {once:true});
    });
    this.socket.addEventListener("message", event => {
      let message;
      try { message = JSON.parse(String(event.data)); } catch { return; }
      const pending = this.pending.get(message.id);
      if (!pending) return;
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
      }, 3000);
      this.pending.set(id, {resolve, reject, timer});
      this.socket.send(JSON.stringify({id, method, params}));
    });
  }

  close() {
    try { this.socket?.close(); } catch { /* conexão efêmera */ }
  }
}

async function activePageTarget() {
  const response = await fetch("http://127.0.0.1:9223/json/list", {signal:AbortSignal.timeout(3000)});
  if (!response.ok) throw new Error(`CDP respondeu HTTP ${response.status}`);
  const targets = (await response.json()).filter(target => target.type === "page"
    && target.webSocketDebuggerUrl
    && !String(target.url || "").startsWith("chrome://"));
  if (!targets.length) throw new Error("nenhuma página web controlável está aberta");
  const inspected = [];
  for (const target of targets) {
    const client = new CdpClient(target.webSocketDebuggerUrl);
    try {
      await client.open();
      const response = await client.send("Runtime.evaluate", {
        expression:"JSON.stringify({focused:document.hasFocus(),visibility:document.visibilityState})",
        returnByValue:true,
      });
      inspected.push({target, state:JSON.parse(response?.result?.value || "{}")});
    } catch { /* uma aba encerrada durante a inspeção é ignorada */ }
    finally { client.close(); }
  }
  if (!inspected.length) throw new Error("nenhuma página web respondeu ao CDP");
  const normalizeTitle = value => String(value || "")
    .replace(/\s*[:\-–—]\s*(Google Chrome for Testing|Chromium|Google Chrome)\s*$/i, "")
    .trim()
    .toLocaleLowerCase();
  const activeTitle = normalizeTitle(activeWindowTitle);
  inspected.sort((left, right) => {
    const score = item => {
      const targetTitle = normalizeTitle(item.target.title);
      const titleMatch = activeTitle && targetTitle
        && (activeTitle === targetTitle || activeTitle.startsWith(targetTitle) || targetTitle.startsWith(activeTitle));
      return (titleMatch ? 1000 : 0)
      + (item.state.focused ? 100 : 0)
      + (item.state.visibility === "visible" ? 20 : 0)
      + (item.target.url !== "about:blank" ? 5 : 0);
    };
    return score(right) - score(left);
  });
  return inspected[0].target;
}

async function main() {
  const target = await activePageTarget();
  const client = new CdpClient(target.webSocketDebuggerUrl);
  try {
    await client.open();
    const expression = `(() => {
      const selector = [
        'textarea:not([disabled]):not([readonly])',
        'input:not([disabled]):not([readonly]):not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="color"]):not([type="file"]):not([type="hidden"])',
        'select:not([disabled])',
        '[contenteditable="true"]',
        '[contenteditable=""]',
        '[role="textbox"]',
        '[role="searchbox"]',
        '[role="combobox"]',
      ].join(',');
      const active = document.activeElement;
      const editable = active?.matches?.(selector) ? active : active?.closest?.(selector);
      return JSON.stringify({
        editable:Boolean(editable),
        element:String(active?.tagName || '').toLowerCase(),
        remoteX:${JSON.stringify(remoteX)},
        remoteY:${JSON.stringify(remoteY)},
        documentFocused:document.hasFocus(),
      });
    })()`;
    const response = await client.send("Runtime.evaluate", {expression, returnByValue:true});
    const result = JSON.parse(response?.result?.value || "{}");
    console.log(JSON.stringify({ok:true, target_id:target.id, ...result}));
  } finally {
    client.close();
  }
}

main().catch(error => {
  console.error(String(error?.message || error));
  process.exitCode = 1;
});
