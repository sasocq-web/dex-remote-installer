(() => {
  const screen = document.getElementById("screen");
  const status = document.getElementById("status");
  const target = new URLSearchParams(location.search).get("target") || "codex";
  const threadId = new URLSearchParams(location.search).get("thread_id") || "";
  const allowed = new Set(["codex", "projects", "desktop", "playwright", "jogos", "android"]);
  let rfb = null;
  let retry = null;
  let connectDeadline = null;
  let fitTimers = [];
  const trackpadPointer = {x:null, y:null};
  const processedKeyboardMessages = new Set();
  let customTouchBridge = false;

  function report(stage, detail = "") {
    const safeStage = String(stage || "unknown").replace(/[^a-z0-9_-]/gi, "-").slice(0, 48);
    const safeDetail = String(detail || "").replace(/[\r\n]/g, " ").slice(0, 160);
    console.debug("[visor remoto]", safeStage, safeDetail);
    window.parent.postMessage({type:"sasocq-remote-diagnostic", stage:safeStage, detail:safeDetail}, location.origin);
  }

  function setStatus(message, kind = "") {
    status.textContent = message;
    status.className = kind;
    window.parent.postMessage({type:"sasocq-remote-status", message:String(message || ""), kind:String(kind || "")}, location.origin);
  }

  function forceFitRemoteViewport(reportFit = false) {
    if (!rfb) return false;
    const width = Math.max(1, screen.clientWidth);
    const height = Math.max(1, screen.clientHeight);
    rfb.scaleViewport = true;
    // noVNC normally reacts through ResizeObserver. Mobile iframe/fullscreen
    // transitions can occur before the framebuffer has its final size, which
    // leaves the 1440px canvas at 1:1 and cuts the login form off-screen.
    if (rfb._display && typeof rfb._display.autoscale === "function") {
      rfb._display.autoscale(width, height);
      if (typeof rfb._fixScrollbars === "function") rfb._fixScrollbars();
    } else if (typeof rfb._updateScale === "function") {
      rfb._updateScale();
    }
    const canvas = trackpadCanvas();
    if (canvas) {
      canvas.style.maxWidth = "100%";
      canvas.style.maxHeight = "100%";
    }
    if (reportFit) {
      report("viewport-fit", `${width}x${height};fb=${canvas?.width || 0}x${canvas?.height || 0}`);
    }
    return true;
  }

  function scheduleRemoteViewportFit(reportFit = false) {
    fitTimers.forEach(clearTimeout);
    fitTimers = [0, 100, 350, 900, 1800].map((delay, index) => setTimeout(
      () => forceFitRemoteViewport(reportFit && index === 4),
      delay,
    ));
  }

  function websocketUrl() {
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const params = new URLSearchParams();
    if (target !== "codex") params.set("target", target);
    if (target === "playwright" && threadId) params.set("thread_id", threadId);
    const query = params.size ? `?${params.toString()}` : "";
    return `${scheme}//${location.host}/api/remote-desktop/ws${query}`;
  }

  function connect(RFB) {
    clearTimeout(retry);
    clearTimeout(connectDeadline);
    screen.replaceChildren();
    setStatus("Conectando à sessão segura…");
    publishKeyboardState(false);
    const url = websocketUrl();
    report("websocket-create", url.replace(location.host, "host"));
    rfb = new RFB(screen, url, {shared: true});
    trackpadPointer.x = null;
    trackpadPointer.y = null;
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.showDotCursor = true;
    rfb.addEventListener("connect", () => {
      clearTimeout(connectDeadline);
      report("connected");
      setStatus("Conectado", "connected");
      publishKeyboardState(true);
      scheduleRemoteViewportFit(true);
      window.parent.postMessage({
        type:"sasocq-remote-pointer-capabilities",
        relative:true,
        scrollX:true,
        scrollY:true,
        gestures:"one-finger-pointer,two-finger-scroll,pinch-zoom",
      }, location.origin);
    });
    rfb.addEventListener("disconnect", event => {
      clearTimeout(connectDeadline);
      const clean = Boolean(event.detail && event.detail.clean);
      publishKeyboardState(false);
      report("disconnected", clean ? "clean" : "unclean");
      setStatus(clean ? "Sessão encerrada" : "Reconectando…", clean ? "" : "error");
      if (!clean) retry = setTimeout(() => connect(RFB), 1500);
    });
    connectDeadline = setTimeout(() => {
      report("connect-timeout");
      setStatus("A transmissão não respondeu. Tentando novamente…", "error");
    }, 8000);
    window.sasocqRfb = rfb;
  }

  function keyboardReady() {
    return Boolean(rfb && rfb._rfbConnectionState === "connected" && !rfb.viewOnly);
  }

  function publishKeyboardState(ready = keyboardReady()) {
    window.parent.postMessage({type:"sasocq-remote-keyboard-state", ready:Boolean(ready)}, location.origin);
  }

  function acknowledgeKeyboard(id, sent) {
    window.parent.postMessage({type:"sasocq-remote-keyboard-ack", id:String(id || ""), sent:Boolean(sent)}, location.origin);
  }

  function sendChord(keysyms) {
    if (!keyboardReady() || !Array.isArray(keysyms) || !keysyms.length) return false;
    keysyms.forEach(keysym => rfb.sendKey(keysym, null, true));
    [...keysyms].reverse().forEach(keysym => rfb.sendKey(keysym, null, false));
    return true;
  }

  const namedKeysyms = {
    Backspace:0xff08,
    Tab:0xff09,
    Enter:0xff0d,
    Escape:0xff1b,
    Delete:0xffff,
    ArrowLeft:0xff51,
    ArrowUp:0xff52,
    ArrowRight:0xff53,
    ArrowDown:0xff54,
    Home:0xff50,
    End:0xff57,
    PageUp:0xff55,
    PageDown:0xff56,
  };

  function sendText(text) {
    if (!keyboardReady()) return false;
    for (const character of String(text || "")) {
      const codepoint = character.codePointAt(0);
      const keysym = codepoint <= 0xff ? codepoint : 0x01000000 | codepoint;
      rfb.sendKey(keysym, null);
    }
    return true;
  }

  function sendNamedKey(key, code) {
    if (!keyboardReady()) return false;
    const name = String(key || "");
    if (name.length === 1) return sendText(name);
    const keysym = namedKeysyms[name];
    if (!keysym) return false;
    rfb.sendKey(keysym, String(code || name));
    return true;
  }

  function trackpadCanvas() {
    return rfb?._canvas || screen.querySelector("canvas");
  }

  function trackpadPosition() {
    const canvas = trackpadCanvas();
    if (!canvas) return null;
    const bounds = canvas.getBoundingClientRect();
    const width = Math.max(2, bounds.width);
    const height = Math.max(2, bounds.height);
    if (!Number.isFinite(trackpadPointer.x) || !Number.isFinite(trackpadPointer.y)) {
      trackpadPointer.x = width / 2;
      trackpadPointer.y = height / 2;
    }
    trackpadPointer.x = Math.max(0, Math.min(width - 1, trackpadPointer.x));
    trackpadPointer.y = Math.max(0, Math.min(height - 1, trackpadPointer.y));
    return {canvas, bounds, x:trackpadPointer.x, y:trackpadPointer.y};
  }

  function publishTrackpadPosition(position) {
    window.parent.postMessage({
      type:"sasocq-remote-pointer-state",
      x:position.bounds.left + position.x,
      y:position.bounds.top + position.y,
    }, location.origin);
  }

  function publishRemoteTap(position) {
    if (!position?.canvas || !position?.bounds?.width || !position?.bounds?.height) return;
    window.parent.postMessage({
      type:"sasocq-remote-tap",
      target,
      x:Math.round(position.x * position.canvas.width / position.bounds.width),
      y:Math.round(position.y * position.canvas.height / position.bounds.height),
    }, location.origin);
  }

  function dispatchCanvasMouse(type, position, buttons = 0, button = 0) {
    if (!position?.canvas) return false;
    const event = new MouseEvent(type, {
      bubbles:true,
      cancelable:true,
      view:window,
      clientX:position.bounds.left + position.x,
      clientY:position.bounds.top + position.y,
      buttons,
      button,
    });
    position.canvas.dispatchEvent(event);
    return true;
  }

  function moveTrackpadPointer(dx, dy) {
    const position = trackpadPosition();
    if (!position || !rfb) return false;
    trackpadPointer.x = Math.max(0, Math.min(position.bounds.width - 1, position.x + dx));
    trackpadPointer.y = Math.max(0, Math.min(position.bounds.height - 1, position.y + dy));
    const next = {...position, x:trackpadPointer.x, y:trackpadPointer.y};
    const sent = dispatchCanvasMouse("mousemove", next, 0, 0);
    publishTrackpadPosition(next);
    return sent;
  }

  function clickTrackpadPointer(button) {
    const position = trackpadPosition();
    if (!position || !rfb) return false;
    const buttons = {left:1, middle:4, right:2};
    const buttonIds = {left:0, middle:1, right:2};
    const name = Object.hasOwn(buttons, button) ? button : "left";
    dispatchCanvasMouse("mousedown", position, buttons[name], buttonIds[name]);
    dispatchCanvasMouse("mouseup", position, 0, buttonIds[name]);
    publishTrackpadPosition(position);
    publishRemoteTap(position);
    return true;
  }

  function scrollTrackpadPointer(dx, dy) {
    const position = trackpadPosition();
    if (!position || !rfb) return false;
    const deltaX = Number(dx) || 0;
    const deltaY = Number(dy) || 0;
    if (!deltaX && !deltaY) return false;
    position.canvas.dispatchEvent(new WheelEvent("wheel", {
      bubbles:true,
      cancelable:true,
      view:window,
      clientX:position.bounds.left + position.x,
      clientY:position.bounds.top + position.y,
      deltaX,
      deltaY,
      deltaMode:0,
    }));
    publishTrackpadPosition(position);
    return true;
  }

  function directPointer(action, clientX, clientY) {
    const canvas = trackpadCanvas();
    if (!canvas || !rfb) return false;
    const bounds = canvas.getBoundingClientRect();
    const x = Math.max(0, Math.min(bounds.width - 1, Number(clientX) - bounds.left));
    const y = Math.max(0, Math.min(bounds.height - 1, Number(clientY) - bounds.top));
    if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
    const position = {canvas, bounds, x, y};
    trackpadPointer.x = x;
    trackpadPointer.y = y;
    if (action === "direct-down") {
      dispatchCanvasMouse("mousemove", position, 0, 0);
      dispatchCanvasMouse("mousedown", position, 1, 0);
    } else if (action === "direct-move") {
      dispatchCanvasMouse("mousemove", position, 1, 0);
    } else if (action === "direct-up" || action === "direct-cancel") {
      dispatchCanvasMouse("mouseup", position, 0, 0);
      if (action === "direct-up") publishRemoteTap(position);
    } else {
      return false;
    }
    publishTrackpadPosition(position);
    return true;
  }

  function suppressNativeTouchGesture(event) {
    if (!customTouchBridge) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }

  for (const name of ["touchstart", "touchmove", "touchend", "touchcancel"]) {
    screen.addEventListener(name, suppressNativeTouchGesture, {capture:true, passive:false});
  }

  window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== window.parent) return;
    if (event.data?.type === "sasocq-remote-pointer") {
      const action = String(event.data.action || "");
      const handled = action.startsWith("direct-")
        ? directPointer(action, Number(event.data.x), Number(event.data.y))
        : action === "move"
        ? moveTrackpadPointer(Number(event.data.dx) || 0, Number(event.data.dy) || 0)
        : action === "scroll"
        ? scrollTrackpadPointer(Number(event.data.dx) || 0, Number(event.data.dy) || 0)
        : action === "click" && clickTrackpadPointer(String(event.data.button || "left"));
      if (!handled) report("pointer-ignored", action);
      return;
    }
    if (event.data?.type === "sasocq-remote-pointer-mode") {
      customTouchBridge = true;
      report("pointer-mode", String(event.data.mode || "direct"));
      return;
    }
    if (event.data?.type === "sasocq-remote-native-keyboard") {
      if (target !== "codex" || !sendChord([0xffe9, 0xffbf])) { // Alt+F2
        report("native-keyboard-ignored", target);
        return;
      }
      const command = "gdbus call --session --dest org.onboard.Onboard --object-path /org/onboard/Onboard/Keyboard --method org.onboard.Onboard.Keyboard.ToggleVisible";
      window.setTimeout(() => sendText(command), 220);
      window.setTimeout(() => sendNamedKey("Enter", "Enter"), 360);
      report("native-keyboard-toggled");
      return;
    }
    if (event.data?.type === "sasocq-remote-keyboard") {
      const id = String(event.data.id || "");
      if (id && processedKeyboardMessages.has(id)) {
        acknowledgeKeyboard(id, true);
        return;
      }
      const sent = Object.hasOwn(event.data, "text")
        ? sendText(event.data.text)
        : sendNamedKey(event.data.key, event.data.code);
      if (sent && id) {
        processedKeyboardMessages.add(id);
        if (processedKeyboardMessages.size > 1000) processedKeyboardMessages.delete(processedKeyboardMessages.values().next().value);
      }
      acknowledgeKeyboard(id, sent);
      report(sent ? "keyboard-sent" : "keyboard-ignored");
      return;
    }
    if (event.data?.type !== "sasocq-remote-window") return;
    const chords = {
      switch:[0xffe9, 0xff09],   // Alt+Tab
      minimize:[0xffe9, 0xffc6], // Alt+F9
      maximize:[0xffe9, 0xffc7], // Alt+F10
      close:[0xffe9, 0xffc1],    // Alt+F4
    };
    const action = String(event.data.action || "");
    if (sendChord(chords[action])) report("window-action", action);
  });

  const handleViewportResize = () => scheduleRemoteViewportFit(false);
  window.addEventListener("resize", handleViewportResize, {passive:true});
  window.addEventListener("orientationchange", handleViewportResize, {passive:true});
  window.visualViewport?.addEventListener("resize", handleViewportResize, {passive:true});
  new ResizeObserver(handleViewportResize).observe(screen);

  window.addEventListener("error", event => {
    report("window-error", event.message || "erro sem mensagem");
    setStatus("Falha JavaScript ao iniciar a transmissão.", "error");
  });
  window.addEventListener("unhandledrejection", event => {
    report("promise-error", event.reason && (event.reason.message || event.reason) || "rejeição sem mensagem");
    setStatus("Falha ao preparar o cliente de transmissão.", "error");
  });

  if (!allowed.has(target)) {
    setStatus("Alvo de transmissão inválido.", "error");
    return;
  }

  setStatus("Carregando cliente de transmissão…");
  publishKeyboardState(false);
  report("script-start");
  import("/novnc/sasocq-20260817-2250/core/rfb.js")
    .then(module => {
      report("module-loaded");
      connect(module.default);
    })
    .catch(error => {
      console.error("Falha ao carregar o cliente VNC", error);
      report("module-error", error && (error.message || error) || "erro sem mensagem");
      setStatus("Falha ao carregar a transmissão. Atualize o visor.", "error");
    });
})();
