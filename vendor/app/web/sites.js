"use strict";

const siteAccessState = {
  data: {sites:[], policies:{}, totals:{sites:0, accesses:0, conversations:0}, refresh:{}},
  policies: new Map(),
  loading: false,
  polling: 0,
};

function normalizeSiteDomain(value) {
  let text = String(value || "").trim().toLowerCase().replace(/[.,);\]}>]+$/, "");
  if (!text) return "";
  try {
    const url = new URL(text.includes("://") ? text : `https://${text}`);
    let domain = String(url.hostname || "").toLowerCase().replace(/\.$/, "");
    if (domain.startsWith("www.")) domain = domain.slice(4);
    if (!domain.includes(".") || ["localhost", "127.0.0.1"].includes(domain)) return "";
    return domain;
  } catch { return ""; }
}

function stringsInSiteValue(value, depth = 0) {
  if (depth > 6 || value == null) return [];
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(item => stringsInSiteValue(item, depth + 1));
  if (typeof value === "object") return Object.values(value).flatMap(item => stringsInSiteValue(item, depth + 1));
  return [];
}

function approvalSiteDomains(approval) {
  const domains = new Set();
  const urlPattern = /https?:\/\/[^\s<>"'`]+/gi;
  const domainPattern = /(?:^|[^\w@])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})(?![\w-])/gi;
  for (const text of stringsInSiteValue(approval?.params || {})) {
    for (const match of text.matchAll(urlPattern)) {
      const domain = normalizeSiteDomain(match[0]);
      if (domain) domains.add(domain);
    }
    for (const match of text.matchAll(domainPattern)) {
      const domain = normalizeSiteDomain(match[1]);
      if (domain) domains.add(domain);
    }
  }
  return [...domains];
}

function siteApprovalDecision(approval) {
  const domains = approvalSiteDomains(approval);
  if (!domains.length) return "";
  const policies = domains.map(domain => siteAccessState.policies.get(domain) || "ask");
  if (policies.includes("block")) {
    if (approval.method === "item/permissions/requestApproval") return "deny-permissions";
    if (["item/commandExecution/requestApproval", "item/fileChange/requestApproval"].includes(approval.method)) return "decline";
    if (approval.method === "mcpServer/elicitation/request") return "cancel-elicitation";
    return "";
  }
  if (!policies.every(policy => policy === "auto")) return "requirePrompt";

  const paramsText = stringsInSiteValue(approval.params || {}).join(" ").toLowerCase();
  if (approval.method === "item/permissions/requestApproval" && /network|domain|host|url|internet|web/.test(paramsText)) return "grant";
  if (approval.method !== "mcpServer/elicitation/request") return "";
  const readOnlyNavigation = /browser_(?:navigate|open|snapshot|screenshot|find|search|tabs)|\b(?:open|navigate|read|get|fetch|search|snapshot|screenshot)\b/.test(paramsText);
  const sensitive = /\b(?:click|type|fill|submit|upload|download|delete|remove|purchase|buy|checkout|pay|send|message|post|put|patch|login|sign[ -]?in|auth|oauth)\b/.test(paramsText);
  return readOnlyNavigation && !sensitive ? "accept-elicitation" : "requirePrompt";
}

window.automaticSiteAccessApprovalAction = siteApprovalDecision;

async function loadSiteAccessPolicies({policiesOnly = false} = {}) {
  const data = await api(policiesOnly ? "/api/site-access?policies_only=true" : "/api/site-access");
  siteAccessState.data = data;
  siteAccessState.policies = new Map(Object.entries(data.policies || {}));
  for (const site of data.sites || []) siteAccessState.policies.set(site.domain, site.policy || "ask");
  return data;
}

window.loadSiteAccessPolicies = loadSiteAccessPolicies;

function siteAccessDate(value) {
  const numeric = Number(value || 0);
  if (!numeric) return "—";
  const date = new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric);
  return new Intl.DateTimeFormat("pt-BR", {dateStyle:"short", timeStyle:"short"}).format(date);
}

function sitePolicyLabel(policy) {
  return {auto:"Automático", ask:"Perguntar", block:"Bloqueado"}[policy] || "Perguntar";
}

function sitePolicyHelp(policy) {
  return {
    auto:"Navegação e leitura podem continuar sem nova pergunta.",
    ask:"O Dex pede confirmação quando o acesso exigir permissão.",
    block:"Novos pedidos de acesso a este domínio são recusados.",
  }[policy] || "";
}

function siteConversationHTML(conversation) {
  const title = conversation.thread_title || `Conversa ${String(conversation.thread_id || "").slice(0, 8)}…`;
  const project = conversation.project_name || (conversation.workspace === "system" ? "Sistema — Mini PC" : "Projeto");
  const summary = conversation.summary || "Sem resumo disponível para esta conversa.";
  const actions = (conversation.actions || []).join(" • ") || "Acesso registrado";
  return `<article class="site-conversation">
    <div class="site-conversation-head"><div><strong>${escapeHTML(title)}</strong><small>${escapeHTML(project)} • ${escapeHTML(siteAccessDate(conversation.last_access))}</small></div><button class="ghost-button" type="button" data-open-site-thread="${escapeHTML(conversation.thread_id || "")}" data-site-project="${escapeHTML(conversation.project_id || "")}">Abrir conversa</button></div>
    <p>${escapeHTML(summary)}</p><small>${escapeHTML(`${conversation.count || 1} acesso(s) • ${actions}`)}</small>
  </article>`;
}

function siteCardHTML(site) {
  const policy = site.policy || "ask";
  return `<article class="site-access-card" data-site-domain="${escapeHTML(site.domain)}">
    <div class="site-access-card-main">
      <div class="site-favicon" aria-hidden="true">${escapeHTML(site.domain.slice(0, 1).toUpperCase())}</div>
      <div class="site-access-identity"><strong>${escapeHTML(site.domain)}</strong><small>Último acesso ${escapeHTML(siteAccessDate(site.last_access))}</small></div>
      <div class="site-access-metric"><strong>${escapeHTML(String(site.count || 0))}</strong><small>acessos</small></div>
      <div class="site-access-metric"><strong>${escapeHTML(String(site.conversation_count || 0))}</strong><small>conversas</small></div>
      <label class="site-policy-select"><span>Novos acessos</span><select data-site-policy="${escapeHTML(site.domain)}" class="policy-${escapeHTML(policy)}"><option value="auto" ${policy === "auto" ? "selected" : ""}>Automático</option><option value="ask" ${policy === "ask" ? "selected" : ""}>Perguntar</option><option value="block" ${policy === "block" ? "selected" : ""}>Bloquear</option></select><small>${escapeHTML(sitePolicyHelp(policy))}</small></label>
    </div>
    <details class="site-access-conversations"><summary>Ver ${escapeHTML(String(site.conversation_count || 0))} conversa(s) e o que foi feito</summary><div>${(site.conversations || []).map(siteConversationHTML).join("")}</div></details>
  </article>`;
}

function renderSiteAccessPage() {
  const data = siteAccessState.data || {};
  const totals = data.totals || {};
  const query = String(el("site-access-search")?.value || "").trim().toLowerCase();
  const policyFilter = el("site-access-filter")?.value || "all";
  el("site-access-totals").innerHTML = [
    [totals.sites || 0, "sites registrados"],
    [totals.accesses || 0, "acessos observados"],
    [totals.conversations || 0, "conversas relacionadas"],
  ].map(([value, label]) => `<article><strong>${escapeHTML(String(value))}</strong><span>${escapeHTML(label)}</span></article>`).join("");

  const sites = (data.sites || []).filter(site => {
    if (policyFilter !== "all" && (site.policy || "ask") !== policyFilter) return false;
    if (!query) return true;
    const conversationText = (site.conversations || []).map(item => `${item.thread_title || ""} ${item.project_name || ""} ${item.summary || ""}`).join(" ");
    return `${site.domain} ${conversationText}`.toLowerCase().includes(query);
  });
  const list = el("site-access-list");
  if (siteAccessState.loading && !(data.sites || []).length) list.innerHTML = '<div class="site-access-empty"><span class="spinner"></span><strong>Carregando o histórico…</strong></div>';
  else if (!sites.length) list.innerHTML = `<div class="site-access-empty"><strong>${query || policyFilter !== "all" ? "Nenhum resultado" : "Nenhum site registrado ainda"}</strong><span>${query || policyFilter !== "all" ? "Altere a busca ou o filtro." : "Use Revisar histórico para indexar as conversas existentes."}</span></div>`;
  else list.innerHTML = sites.map(siteCardHTML).join("");

  const refresh = data.refresh || {};
  const progress = el("site-access-progress");
  progress.classList.toggle("hidden", !refresh.running && !refresh.error);
  progress.textContent = refresh.error
    ? `A revisão encontrou um erro: ${refresh.error}`
    : refresh.running ? `Revisando ${refresh.threads_scanned || 0} conversa(s) em ${refresh.projects_scanned || 0} projeto(s)…` : "";
  el("site-access-refresh").disabled = Boolean(refresh.running);
  el("site-access-refresh").textContent = refresh.running ? "Revisando…" : "Revisar histórico";
  el("site-access-privacy").textContent = data.privacy || "Somente domínios são armazenados; caminhos e parâmetros das URLs são descartados.";
}

async function refreshSiteAccessData({autoRefresh = false} = {}) {
  siteAccessState.loading = true;
  try {
    const data = await loadSiteAccessPolicies();
    renderSiteAccessPage();
    if (autoRefresh && !data.refresh?.running && !data.refresh?.completed) {
      await api("/api/site-access/refresh", {method:"POST"});
      await loadSiteAccessPolicies();
      renderSiteAccessPage();
    }
    clearTimeout(siteAccessState.polling);
    if (siteAccessState.data.refresh?.running) {
      siteAccessState.polling = window.setTimeout(() => refreshSiteAccessData(), 1200);
    }
  } finally {
    siteAccessState.loading = false;
    renderSiteAccessPage();
  }
}

async function showSiteAccessPage() {
  const dialog = el("site-access-dialog");
  if (!dialog.open) dialog.showModal();
  dialog.querySelector(".site-access-page")?.scrollTo({top:0, behavior:"instant"});
  closeMobilePanels();
  renderSiteAccessPage();
  await refreshSiteAccessData({autoRefresh:true});
}

window.openSiteAccessPage = () => showSiteAccessPage().catch(error => toast(error.message, "error"));
window.renderSiteAccessPage = renderSiteAccessPage;

el("site-access-close")?.addEventListener("click", () => el("site-access-dialog")?.close());
el("site-access-dialog")?.addEventListener("cancel", event => { event.preventDefault(); el("site-access-dialog").close(); });
el("site-access-search")?.addEventListener("input", renderSiteAccessPage);
el("site-access-filter")?.addEventListener("change", renderSiteAccessPage);
el("site-access-refresh")?.addEventListener("click", async () => {
  try {
    await api("/api/site-access/refresh", {method:"POST"});
    await refreshSiteAccessData();
  } catch (error) { toast(error.message, "error"); }
});
el("site-access-list")?.addEventListener("change", async event => {
  const select = event.target.closest("[data-site-policy]");
  if (!select) return;
  const domain = select.dataset.sitePolicy;
  const previous = siteAccessState.policies.get(domain) || "ask";
  select.disabled = true;
  try {
    await api(`/api/site-access/${encodeURIComponent(domain)}/policy`, {method:"PUT", body:JSON.stringify({mode:select.value})});
    siteAccessState.policies.set(domain, select.value);
    const site = (siteAccessState.data.sites || []).find(item => item.domain === domain);
    if (site) site.policy = select.value;
    toast(`${domain}: ${sitePolicyLabel(select.value)}.`, "success");
    renderSiteAccessPage();
  } catch (error) {
    select.value = previous;
    toast(error.message, "error");
  } finally { select.disabled = false; }
});
el("site-access-list")?.addEventListener("click", async event => {
  const button = event.target.closest("[data-open-site-thread]");
  if (!button) return;
  const threadId = button.dataset.openSiteThread;
  const projectId = button.dataset.siteProject;
  if (!threadId) return;
  el("site-access-dialog")?.close();
  try {
    if (projectId && state.activeProject?.id !== projectId) await selectProject(projectId);
    await openThread(threadId);
  } catch (error) { toast(error.message, "error"); }
});
