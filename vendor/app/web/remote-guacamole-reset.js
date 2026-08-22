"use strict";

const next = new URLSearchParams(location.search).get("next") || "";
if (!next.startsWith("/guacamole/")) {
  document.body.textContent = "Destino remoto inválido.";
  throw new Error("invalid Guacamole destination");
}
localStorage.removeItem("GUAC_AUTH_TOKEN");
location.replace(next);
