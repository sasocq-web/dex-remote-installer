(() => {
  const frame = document.getElementById("viewer");
  const status = document.getElementById("status");
  const connect = document.getElementById("connect");
  const disconnect = document.getElementById("disconnect");
  connect.addEventListener("click", () => {
    frame.src = "/remote-viewer.html?target=dot&v=dot-view337";
    frame.hidden = false;
    document.getElementById("intro").hidden = true;
    connect.disabled = true;
    disconnect.disabled = false;
    status.textContent = "Conectando à sessão autenticada…";
  });
  disconnect.addEventListener("click", () => {
    frame.src = "about:blank";
    frame.hidden = true;
    document.getElementById("intro").hidden = false;
    connect.disabled = false;
    disconnect.disabled = true;
    status.textContent = "Visor fechado. As conversas e tarefas continuam no aplicativo.";
  });
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.source !== frame.contentWindow) return;
    if (event.data?.type === "sasocq-remote-status") {
      status.textContent = String(event.data.message || "");
      if (event.data.kind === "error") status.textContent += " — verifique a sessão do Dex e a disponibilidade do aplicativo. Feche o visor antes de tentar novamente.";
    }
  });
})();