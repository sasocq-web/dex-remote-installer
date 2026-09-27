"use strict";

const automationPageState = {
  data: {automations:[], totals:{all:0, active:0, paused:0, projects:0}, timezone:"America/Sao_Paulo"},
  loading: false,
};

function automationDate(value) {
  const numeric = Number(value || 0);
  if (!numeric) return "—";
  const date = new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric);
  return new Intl.DateTimeFormat("pt-BR", {dateStyle:"short", timeStyle:"short", timeZone:"America/Sao_Paulo"}).format(date);
}

function automationRuleParts(value) {
  return Object.fromEntries(String(value || "").replace(/^RRULE:/i, "").split(";").map(part => part.split("=", 2)).filter(part => part.length === 2));
}

function automationRuleLabel(value) {
  const rule = automationRuleParts(value);
  const interval = Math.max(1, Number(rule.INTERVAL || 1));
  const minute = String(Number((rule.BYMINUTE || "0").split(",")[0])).padStart(2, "0");
  const hour = String(Number((rule.BYHOUR || "0").split(",")[0])).padStart(2, "0");
  const every = interval > 1 ? `a cada ${interval}` : "todo";
  if (rule.FREQ === "MINUTELY") return interval === 1 ? "A cada minuto" : `A cada ${interval} minutos`;
  if (rule.FREQ === "HOURLY") return `${every} hora${interval > 1 ? "s" : ""}, aos ${minute} min`;
  if (rule.FREQ === "DAILY") return `${interval === 1 ? "Diariamente" : `A cada ${interval} dias`} às ${hour}:${minute}`;
  if (rule.FREQ === "WEEKLY") {
    const days = {MO:"seg", TU:"ter", WE:"qua", TH:"qui", FR:"sex", SA:"sáb", SU:"dom"};
    const selected = String(rule.BYDAY || "").split(",").filter(Boolean).map(day => days[day] || day).join(", ");
    return `${interval === 1 ? "Semanalmente" : `A cada ${interval} semanas`}${selected ? ` • ${selected}` : ""} às ${hour}:${minute}`;
  }
  if (rule.FREQ === "MONTHLY") return `${interval === 1 ? "Mensalmente" : `A cada ${interval} meses`} • dia ${rule.BYMONTHDAY || "—"} às ${hour}:${minute}`;
  return value || "Recorrência não informada";
}

function automationKindLabel(kind) {
  return kind === "cron" ? "Execução independente" : "Continua a conversa";
}

function automationCardHTML(item) {
  const active = item.status === "ACTIVE";
  const title = item.name || "Agendamento sem nome";
  const conversation = item.kind === "cron" ? "Nova conversa a cada execução" : (item.thread_title || `Conversa ${String(item.target_thread_id || "").slice(0, 8)}…`);
  const editable = !state.session?.read_only_automation;
  return `<article class="automation-card" data-automation-id="${escapeHTML(item.id || "")}">
    <div class="automation-card-head">
      <div class="automation-icon ${active ? "active" : "paused"}" aria-hidden="true">◷</div>
      <div class="automation-identity"><div class="automation-title-line"><strong>${escapeHTML(title)}</strong><span class="automation-status ${active ? "active" : "paused"}">${active ? "Ativo" : "Pausado"}</span></div><small>${escapeHTML(item.project_name || "Projeto")} • ${escapeHTML(automationKindLabel(item.kind))}</small></div>
      ${item.target_thread_id ? `<button class="secondary-button automation-open-thread" type="button" data-automation-thread="${escapeHTML(item.target_thread_id)}" data-automation-project="${escapeHTML(item.project_id || "")}">Abrir conversa</button>` : ""}
    </div>
    <p class="automation-prompt">${escapeHTML(item.prompt || "Sem instrução registrada.")}</p>
    <div class="automation-metrics">
      <div><span>Próxima execução</span><strong>${escapeHTML(active ? automationDate(item.next_run_at) : "Pausado")}</strong></div>
      <div><span>Recorrência</span><strong>${escapeHTML(automationRuleLabel(item.rrule))}</strong></div>
      <div><span>Última execução</span><strong>${escapeHTML(automationDate(item.last_run_at))}</strong></div>
      <div><span>Destino</span><strong>${escapeHTML(conversation)}</strong></div>
    </div>
    <div class="automation-card-foot"><code>${escapeHTML(item.rrule || "")}</code>${item.model ? `<span>Modelo ${escapeHTML(item.model)}</span>` : ""}</div>
    ${editable ? `<div class="automation-actions">
      <button class="secondary-button" type="button" data-automation-action="toggle" data-automation-id="${escapeHTML(item.id)}">${active ? "Pausar" : "Retomar"}</button>
      <button class="secondary-button" type="button" data-automation-action="edit" data-automation-id="${escapeHTML(item.id)}">Editar</button>
      <button class="danger-button" type="button" data-automation-action="cancel" data-automation-id="${escapeHTML(item.id)}">Cancelar</button>
    </div>` : ""}
  </article>`;
}

function populateAutomationProjects() {
  const select = el("automation-project-filter");
  if (!select) return;
  const selected = select.value || "all";
  const projects = new Map();
  for (const item of automationPageState.data.automations || []) projects.set(item.project_id || "", item.project_name || item.project_id || "Projeto");
  select.innerHTML = '<option value="all">Todos os projetos</option>' + [...projects.entries()].sort((a, b) => a[1].localeCompare(b[1], "pt-BR")).map(([id, name]) => `<option value="${escapeHTML(id)}">${escapeHTML(name)}</option>`).join("");
  select.value = [...projects.keys()].includes(selected) ? selected : "all";
}

function renderAutomationPage() {
  const data = automationPageState.data || {};
  const totals = data.totals || {};
  el("automation-totals").innerHTML = [
    [totals.all || 0, "agendamentos"],
    [totals.active || 0, "ativos"],
    [totals.paused || 0, "pausados"],
    [totals.projects || 0, "projetos"],
  ].map(([value, label]) => `<article><strong>${escapeHTML(String(value))}</strong><span>${escapeHTML(label)}</span></article>`).join("");
  populateAutomationProjects();
  const query = String(el("automation-search")?.value || "").trim().toLowerCase();
  const status = el("automation-status-filter")?.value || "all";
  const project = el("automation-project-filter")?.value || "all";
  const filtered = (data.automations || []).filter(item => {
    if (status !== "all" && item.status !== status) return false;
    if (project !== "all" && item.project_id !== project) return false;
    if (!query) return true;
    return `${item.name || ""} ${item.prompt || ""} ${item.project_name || ""} ${item.thread_title || ""}`.toLowerCase().includes(query);
  });
  const list = el("automation-list");
  if (automationPageState.loading && !(data.automations || []).length) {
    list.innerHTML = '<div class="site-access-empty"><span class="spinner"></span><strong>Carregando agendamentos…</strong></div>';
  } else if (!filtered.length) {
    const filtering = query || status !== "all" || project !== "all";
    list.innerHTML = `<div class="site-access-empty automation-empty"><span aria-hidden="true">◷</span><strong>${filtering ? "Nenhum resultado" : "Nenhum agendamento criado ainda"}</strong><p>${filtering ? "Altere a busca ou os filtros." : "Peça ao Codex para agendar uma tarefa; ela aparecerá aqui automaticamente."}</p></div>`;
  } else {
    list.innerHTML = filtered.map(automationCardHTML).join("");
  }
  el("automation-timezone").textContent = `Horários exibidos em São Paulo (${data.timezone || "America/Sao_Paulo"}).`;
}

async function updateAutomationFromPage(item, changes) {
  const result = await api(`/api/automations/${encodeURIComponent(item.id)}`, {method:"PATCH", body:JSON.stringify(changes)});
  await refreshAutomationPage();
  return result;
}

async function editAutomation(item) {
  const dialog = el("automation-edit-dialog");
  el("automation-edit-title").textContent = item.name || "Agendamento";
  const modelSelect = el("automation-edit-model");
  modelSelect.innerHTML = '<option value="">Padrão da conversa</option>' + (item.model ? `<option value="${escapeHTML(item.model)}">${escapeHTML(item.model)} (atual)</option>` : "");
  modelSelect.value = item.model || "";
  el("automation-edit-rrule").value = item.rrule || "";
  el("automation-edit-kind").value = item.kind || "heartbeat";
  el("automation-edit-kind").querySelector('option[value="heartbeat"]').disabled = !item.target_thread_id;
  el("automation-edit-rule-preview").textContent = automationRuleLabel(item.rrule);
  dialog.dataset.automationId = item.id;
  dialog.showModal();
  try {
    const workspace = item.project_kind === "system" ? "system" : "project";
    const params = new URLSearchParams({workspace});
    if (workspace === "project") params.set("project_id", item.project_id);
    const result = await api(`/api/models?${params}`);
    if (dialog.dataset.automationId !== item.id || !dialog.open) return;
    modelSelect.innerHTML = '<option value="">Padrão da conversa</option>' + (result.data || []).filter(model => !model.hidden).map(model =>
      `<option value="${escapeHTML(model.id || model.model || "")}">${escapeHTML(model.displayName || model.model || model.id || "")}</option>`
    ).join("");
    if (item.model && ![...modelSelect.options].some(option => option.value === item.model)) {
      modelSelect.insertAdjacentHTML("beforeend", `<option value="${escapeHTML(item.model)}">${escapeHTML(item.model)} (atual)</option>`);
    }
    modelSelect.value = item.model || "";
  } catch (error) {
    toast(`Lista de modelos indisponível: ${error.message}`, "error");
  }
}

async function refreshAutomationPage() {
  automationPageState.loading = true;
  renderAutomationPage();
  try {
    automationPageState.data = await api("/api/automations");
  } finally {
    automationPageState.loading = false;
    renderAutomationPage();
  }
}

async function showAutomationPage() {
  const dialog = el("automation-dialog");
  if (!dialog.open) dialog.showModal();
  dialog.querySelector(".automation-page")?.scrollTo({top:0, behavior:"instant"});
  closeMobilePanels();
  await refreshAutomationPage();
}

window.openAutomationPage = () => showAutomationPage().catch(error => toast(error.message, "error"));

el("automation-close")?.addEventListener("click", () => el("automation-dialog")?.close());
el("automation-dialog")?.addEventListener("cancel", event => { event.preventDefault(); el("automation-dialog").close(); });
el("automation-search")?.addEventListener("input", renderAutomationPage);
el("automation-status-filter")?.addEventListener("change", renderAutomationPage);
el("automation-project-filter")?.addEventListener("change", renderAutomationPage);
el("automation-refresh")?.addEventListener("click", () => refreshAutomationPage().catch(error => toast(error.message, "error")));
el("automation-edit-close")?.addEventListener("click", () => el("automation-edit-dialog")?.close());
el("automation-edit-rrule")?.addEventListener("input", event => {
  el("automation-edit-rule-preview").textContent = automationRuleLabel(event.target.value);
});
el("automation-edit-form")?.addEventListener("submit", async event => {
  event.preventDefault();
  const item = (automationPageState.data.automations || []).find(entry => entry.id === el("automation-edit-dialog").dataset.automationId);
  if (!item) return;
  const button = el("automation-edit-save");
  button.disabled = true;
  try {
    const model = el("automation-edit-model").value;
    await updateAutomationFromPage(item, {
      model,
      rrule:el("automation-edit-rrule").value.trim(),
      kind:el("automation-edit-kind").value,
    });
    el("automation-edit-dialog").close();
    toast("Agendamento atualizado.");
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; }
});
el("automation-list")?.addEventListener("click", async event => {
  const action = event.target.closest("[data-automation-action]");
  if (action) {
    const item = (automationPageState.data.automations || []).find(entry => entry.id === action.dataset.automationId);
    if (!item || state.session?.read_only_automation) return;
    if (action.dataset.automationAction === "edit") return editAutomation(item);
    if (action.dataset.automationAction === "cancel" && !confirm(`Cancelar o agendamento “${item.name}”? As próximas execuções serão removidas.`)) return;
    action.disabled = true;
    try {
      if (action.dataset.automationAction === "toggle") {
        await updateAutomationFromPage(item, {status:item.status === "ACTIVE" ? "PAUSED" : "ACTIVE"});
        toast(item.status === "ACTIVE" ? "Agendamento pausado." : "Agendamento retomado.");
      } else if (action.dataset.automationAction === "cancel") {
        await api(`/api/automations/${encodeURIComponent(item.id)}`, {method:"DELETE"});
        await refreshAutomationPage();
        toast("Agendamento cancelado.");
      }
    } catch (error) { action.disabled = false; toast(error.message, "error"); }
    return;
  }
  const button = event.target.closest("[data-automation-thread]");
  if (!button) return;
  el("automation-dialog")?.close();
  try {
    const projectId = button.dataset.automationProject;
    if (projectId && state.activeProject?.id !== projectId) await selectProject(projectId);
    await openThread(button.dataset.automationThread);
  } catch (error) { toast(error.message, "error"); }
});
