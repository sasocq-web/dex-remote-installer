"use strict";

const DEFAULT_BACKUP_REMOTE_PATH = "SASOCQ/Backups/Servidor";
const PROJECT_USAGE_STORAGE_KEY = "codex-linux-control.project-usage.v1";
const LEGACY_COMPOSER_PREFERENCES_STORAGE_KEY = "codex-linux-control.composer-preferences.v1";
const COMPOSER_PREFERENCES_STORAGE_KEY = "codex-linux-control.composer-preferences.v2";
const PROJECT_CATALOG_CACHE_KEY = "codex-linux-control.projects-cache.v1";
const THREAD_SUMMARY_CACHE_KEY = "codex-linux-control.thread-summaries.v1";
const THREAD_TERMINAL_STATUS_KEY = "codex-linux-control.thread-terminal-statuses.v1";
const CONVERSATION_APPROVAL_RULES_KEY = "codex-linux-control.conversation-approval-rules.v1";
const SYSTEM_UPDATE_AUTOMATIC_KEY = "codex-linux-control.system-update-automatic.v1";
const RECENT_PROJECT_LIMIT = 5;
const PROJECT_CONVERSATION_LIMIT = 100;
const THREAD_SEARCH_RESULT_LIMIT = 100;
const THREAD_SEARCH_DEBOUNCE_MS = 280;

function loadCachedProjects() {
  try {
    const value = JSON.parse(localStorage.getItem(PROJECT_CATALOG_CACHE_KEY) || "[]");
    return Array.isArray(value) ? value : [];
  } catch { return []; }
}

function loadCachedThreadSummaries() {
  try {
    const value = JSON.parse(localStorage.getItem(THREAD_SUMMARY_CACHE_KEY) || "{}");
    return new Map(Object.entries(value).map(([projectId, threads]) => [projectId, Array.isArray(threads) ? threads : []]));
  } catch { return new Map(); }
}

function loadThreadTerminalStatuses() {
  try {
    const saved = JSON.parse(localStorage.getItem(THREAD_TERMINAL_STATUS_KEY) || "{}");
    const cutoff = Date.now() - (7 * 24 * 60 * 60 * 1000);
    return new Map(Object.entries(saved).filter(([, value]) => Number(value?.at || 0) >= cutoff));
  } catch { return new Map(); }
}

function loadProjectUsage() {
  try {
    const saved = JSON.parse(localStorage.getItem(PROJECT_USAGE_STORAGE_KEY) || "{}");
    return new Map(Object.entries(saved).map(([projectId, timestamp]) => [projectId, Number(timestamp) || 0]));
  } catch {
    return new Map();
  }
}

function emptyComposerPreferences(networkAccess = "enabled") {
  return {model:"", effort:"", serviceTier:"", networkAccess, updatedAt:0};
}

function normalizeComposerPreferences(value, networkAccess = "enabled") {
  const saved = value && typeof value === "object" ? value : {};
  return {
    model: String(saved.model || ""),
    effort: String(saved.effort || ""),
    serviceTier: String(saved.serviceTier || saved.service_tier || ""),
    networkAccess: networkAccess === "restricted" ? "restricted" : "enabled",
    updatedAt: Math.max(0, Number(saved.updatedAt || saved.updated_at || 0)),
  };
}

function loadComposerPreferenceStore() {
  let contexts = {};
  let networkAccess = "enabled";
  try {
    const saved = JSON.parse(localStorage.getItem(COMPOSER_PREFERENCES_STORAGE_KEY) || "{}");
    contexts = saved.contexts && typeof saved.contexts === "object" ? saved.contexts : {};
    networkAccess = saved.networkAccess === "restricted" ? "restricted" : "enabled";
  } catch { /* A store inválida é reconstruída a partir dos padrões. */ }
  let legacy = null;
  try {
    const saved = JSON.parse(localStorage.getItem(LEGACY_COMPOSER_PREFERENCES_STORAGE_KEY) || "null");
    if (saved && typeof saved === "object") {
      legacy = normalizeComposerPreferences(saved, saved.networkAccess);
      networkAccess = legacy.networkAccess;
    }
  } catch { /* A preferência antiga pode ser descartada com segurança. */ }
  return {contexts, networkAccess, legacy};
}

function composerPreferenceContextKey(project) {
  if (!project?.id) return "";
  const codex = project.kind === "system" ? "system" : "projects";
  return `${codex}:${project.id}`;
}

const initialComposerPreferenceStore = loadComposerPreferenceStore();

function loadConversationApprovalRules() {
  try {
    const saved = JSON.parse(localStorage.getItem(CONVERSATION_APPROVAL_RULES_KEY) || "[]");
    return new Set(Array.isArray(saved) ? saved.map(String) : []);
  } catch { return new Set(); }
}

function backupRemotePath() {
  return el("backup-cloud-remote-path")?.value.trim()
    || el("setup-backup-cloud-remote-path")?.value.trim()
    || state.lastStatus?.backup_cloud?.remote_path
    || state.setup.data?.backup_cloud?.remote_path
    || DEFAULT_BACKUP_REMOTE_PATH;
}

const state = {
  installMode: "full",
  csrf: "",
  identity: "",
  session: null,
  projects: loadCachedProjects(),
  projectActivity: new Map(),
  projectUsage: loadProjectUsage(),
  projectDirectories: [],
  projectRoots: [],
  projectRoot: "",
  projectFolderPath: "",
  projectFolderParent: "",
  projectRootBrowserPath: "",
  projectRootBrowserParent: "",
  models: [],
  composerPreferenceStore: initialComposerPreferenceStore,
  composerPreferenceSyncs: new Map(),
  composerPreferences: emptyComposerPreferences(initialComposerPreferenceStore.networkAccess),
  threads: [],
  projectThreads: loadCachedThreadSummaries(),
  threadTerminalStatuses: loadThreadTerminalStatuses(),
  threadDetails: new Map(),
  messageRenderLimit: 100,
  threadLoadGeneration: 0,
  threadSearch: {query:"", results:[], loading:false, error:"", generation:0, timer:null},
  conversationViewGeneration: 0,
  conversationAutoFollow: true,
  conversationScrollInteractionUntil: 0,
  conversationScrollInteractionVersion: 0,
  conversationRenderTimer: null,
  conversationScrollRestoreFrame: null,
  browserPreviewGroupId: "",
  activeProject: null,
  activeThreadId: null,
  activeTurnId: null,
  activeTurnStartedAt: 0,
  executionActivityAt: 0,
  executionTicker: null,
  turnSubmissionPending: false,
  items: new Map(),
  approvals: new Map(),
  conversationApprovalRules: loadConversationApprovalRules(),
  activity: [],
  diff: "",
  socket: null,
  socketTimer: null,
  socketRetry: 0,
  accounts: {system:null, projects:null},
  rateLimits: {system:null, projects:null},
  bridges: {
    system: {initialized:false, last_error:""},
    projects: {initialized:false, last_error:""},
  },
  loginWorkspace: "system",
  deferredInstall: null,
  mainInterfaceReady: false,
  pendingNotificationTarget: null,
  bridgeReady: false,
  lastStatus: null,
  extensions: null,
  extensionsLoading: false,
  extensionsError: "",
  toolProfile: {skills: [], apps: [], mcp_servers: [], browser: false, desktop: false, system_admin: false},
  toolsTab: "recommended",
  toolsScope: "thread",
  composerMode: null,
  pluginSearch: "",
  remote: {
    status: null,
    dialogOpen: false,
    resizeTimer: null,
    launchBrowserOnOpen: false,
    frameReady: false,
    target: "codex",
    pointerMode: "direct",
    interfaceScale: "auto",
    bridgeTimer: null,
    cursorX: 0,
    cursorY: 0,
    viewportZoom: 1,
    viewportPanX: 0,
    viewportPanY: 0,
    toolbarDrag: null,
    keyboardViewerReady: false,
    keyboardQueue: [],
    keyboardInFlight: null,
    keyboardAckTimer: null,
    keyboardSequence: 0,
    keyboardComposing: false,
    keyboardCompositionTimer: null,
    keyboardCompositionText: "",
    keyboardMode: "none",
    keyboardAutoHideTimer: null,
    mobileLayout: false,
    mobileLayoutPending: false,
    opening: false,
    lastResizeSignature: "",
  },
  devices: [],
  enrollmentRequests: [],
  localNetworkAdmin: false,
  pairingTimer: null,
  setup: {
    active: false,
    step: 0,
    data: null,
    remoteChoice: "local",
    cloudChoice: "onedrive",
    cloudChoiceTouched: false,
    cloudStrategy: "path1",
    startAtLogin: true,
  },
  taskPoll: null,
  projectMenu: null,
  activeTaskKind: "",
  activeTaskCreatedAt: 0,
  settingsPage: "overview",
  terminalSocket: null,
  terminalWorkspace: null,
  backupFolderPath: "",
  backupFolderParent: "",
  oneDriveFolderMode: "backup",
  pcResources: null,
  pcResourceTimer: null,
  systemUpdate: null,
  systemUpdateAutomatic: localStorage.getItem(SYSTEM_UPDATE_AUTOMATIC_KEY) !== "false",
  systemUpdateAutomaticTimer: null,
  systemUpdateStarting: false,
  systemUpdateProgressDismissed: false,
};

const el = id => document.getElementById(id);
const selectors = {
  sidebar: el("sidebar"), inspector: el("inspector"), projectList: el("project-list"),
  threadList: el("thread-list"), chat: el("chat"), messageList: el("message-list"),
  empty: el("empty-state"), prompt: el("prompt"), send: el("send-button"),
  interrupt: el("interrupt-button"), model: el("model-select"), effort: el("effort-select"), speed: el("speed-select"),
  network: el("network-select"), title: el("active-title"), projectLabel: el("active-project-label"),
  status: el("status-pill"), connectionLabel: el("connection-label"), loginBanner: el("login-banner"),
  approvalList: el("approval-list"), approvalCount: el("approval-count"), diffView: el("diff-view"),
  otherApprovalPopups: el("other-conversation-approval-popups"),
  activityList: el("activity-list"), loginDialog: el("login-dialog"), settingsDialog: el("settings-dialog"),
  toastContainer: el("toast-container"), setupOverlay: el("setup-overlay"), setupBody: el("setup-body"),
  setupBack: el("setup-back"), setupNext: el("setup-next"), setupRefresh: el("setup-refresh"),
  taskDialog: el("task-dialog"), taskTitle: el("task-title"), taskMessage: el("task-message"),
  taskLogs: el("task-logs"), taskActionLink: el("task-action-link"), taskSpinner: el("task-spinner"),
  taskProgress: el("task-progress"), taskProgressPhase: el("task-progress-phase"),
  taskProgressPercent: el("task-progress-percent"), taskProgressFill: el("task-progress-fill"),
  taskProgressElapsed: el("task-progress-elapsed"),
  taskDone: el("task-done"), taskClose: el("close-task"), settingsContent: el("settings-content"),
  toolsDialog: el("tools-dialog"), toolsContent: el("tools-content"), toolsSummary: el("tools-summary"),
  toolCount: el("tool-count"), composerTools: el("composer-tools"),
  deviceAuthOverlay: el("device-auth-overlay"), deviceAuthMessage: el("device-auth-message"),
  deviceAuthProgress: el("device-auth-progress"), remoteDialog: el("remote-dialog"),
  remoteEnvironmentDialog: el("remote-environment-dialog"), remoteProjectSelect: el("remote-project-select"),
  remoteFrame: el("remote-frame"), remotePlaceholder: el("remote-placeholder"),
  remoteLiveStatus: el("remote-live-status"), remoteSummaryStatus: el("remote-summary-status"),
  remoteProfile: el("remote-profile"), remoteTarget: el("remote-target"),
  remotePointerMode: el("remote-pointer-mode"), remoteInterfaceScale: el("remote-interface-scale"),
  remoteKeyboardPanel: el("remote-keyboard-panel"), remoteKeyboardProxy: el("remote-keyboard-proxy"), pairingDialog: el("pairing-dialog"),
  homeCodexUsage: el("home-codex-usage"),
  codexUsageSummary: el("codex-usage-summary"),
  pairingQr: el("pairing-qr"), pairingLink: el("pairing-link"), pairingExpiry: el("pairing-expiry"),
  projectDialog: el("project-dialog"), projectManagerList: el("project-manager-list"),
  projectFolderList: el("project-folder-list"),
  projectRootDialog: el("project-root-dialog"), projectRootBrowserList: el("project-root-browser-list"),
  terminalDialog: el("terminal-dialog"), terminalOutput: el("terminal-output"),
  terminalInput: el("terminal-input"), terminalState: el("terminal-state"),
  backupFolderDialog: el("backup-folder-dialog"), backupFolderList: el("backup-folder-list"),
  pcDataDialog: el("pc-data-dialog"), pcDataContent: el("pc-data-content"),
};

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
}

function formatTime(timestamp) {
  if (!timestamp) return "";
  const millis = timestamp > 1e12 ? timestamp : timestamp * 1000;
  try { return new Intl.DateTimeFormat("pt-BR", {dateStyle:"short", timeStyle:"short"}).format(new Date(millis)); }
  catch { return ""; }
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / (1024 ** index)).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function pcResourceState(data) {
  const gib = 1024 ** 3;
  const cpuPercent = Number(data?.cpu?.percent || 0);
  const memory = data?.memory || {};
  const memoryTotal = Number(memory.total || 0);
  const memoryAvailable = Number(memory.available || 0);
  const memoryRatio = memoryTotal ? memoryAvailable / memoryTotal : 1;
  const rootDisk = (data?.filesystems || []).find(item => item.path === "/") || {};
  const diskTotal = Number(rootDisk.total || 0);
  const diskFree = Number(rootDisk.free || 0);
  const diskRatio = diskTotal ? diskFree / diskTotal : 1;
  const warnings = [];
  let danger = false;
  if (cpuPercent >= 85) {
    warnings.push(`CPU em ${cpuPercent.toFixed(0)}%`);
    danger ||= cpuPercent >= 95;
  }
  if (memoryTotal && (memoryAvailable < 2 * gib || memoryRatio < .15)) {
    warnings.push(`RAM disponível: ${formatBytes(memoryAvailable)}`);
    danger ||= memoryAvailable < gib || memoryRatio < .08;
  }
  if (diskTotal && (diskFree < 20 * gib || diskRatio < .10)) {
    warnings.push(`SSD livre: ${formatBytes(diskFree)}`);
    danger ||= diskFree < 5 * gib || diskRatio < .05;
  }
  return {cpuPercent, memory, memoryRatio, rootDisk, diskRatio, warnings, danger};
}

function resourceCardClass(warning, danger) {
  return danger ? "danger" : warning ? "warning" : "";
}

function renderPCResources(data) {
  if (!data) return;
  state.pcResources = data;
  const health = pcResourceState(data);
  const alert = el("resource-alert");
  const alertMessage = el("resource-alert-message");
  if (alert && alertMessage) {
    alert.classList.toggle("hidden", !health.warnings.length);
    alert.classList.toggle("danger", health.danger);
    alertMessage.textContent = health.warnings.join(" • ");
  }
  if (!selectors.pcDataContent) return;
  const memoryAvailable = Number(health.memory.available || 0);
  const memoryTotal = Number(health.memory.total || 0);
  const diskFree = Number(health.rootDisk.free || 0);
  const diskTotal = Number(health.rootDisk.total || 0);
  const ramWarning = memoryTotal && (memoryAvailable < 2 * 1024 ** 3 || health.memoryRatio < .15);
  const ramDanger = memoryAvailable < 1024 ** 3 || health.memoryRatio < .08;
  const diskWarning = diskTotal && (diskFree < 20 * 1024 ** 3 || health.diskRatio < .10);
  const diskDanger = diskFree < 5 * 1024 ** 3 || health.diskRatio < .05;
  const hottest = (data.temperatures || []).reduce((best, item) => Number(item.celsius || 0) > Number(best.celsius || 0) ? item : best, {});
  const hardware = data.hardware || {};
  const disk = (hardware.internal_disks || hardware.disks || [])[0] || {};
  const uptimeDays = Number(data.uptime_seconds || 0) / 86400;
  selectors.pcDataContent.innerHTML = `
    <article class="pc-resource-card ${resourceCardClass(health.cpuPercent >= 85, health.cpuPercent >= 95)}"><span>CPU atual</span><strong>${escapeHTML(health.cpuPercent.toFixed(1))}%</strong><small>${escapeHTML(data.cpu?.logical || "—")} threads${hottest.celsius ? ` • ${escapeHTML(hottest.celsius)} °C` : ""}</small></article>
    <article class="pc-resource-card ${resourceCardClass(ramWarning, ramDanger)}"><span>RAM disponível</span><strong>${formatBytes(memoryAvailable)}</strong><small>${formatBytes(Number(health.memory.used || 0))} em uso de ${formatBytes(memoryTotal)}</small></article>
    <article class="pc-resource-card ${resourceCardClass(diskWarning, diskDanger)}"><span>SSD livre</span><strong>${formatBytes(diskFree)}</strong><small>${escapeHTML(health.rootDisk.percent ?? "—")}% usado de ${formatBytes(diskTotal)}</small></article>
    <div class="pc-data-details"><strong>${escapeHTML(data.hostname || "Mini PC SASOCQ")}</strong><span>${escapeHTML(hardware.cpu?.model || data.platform || "Processador não identificado")}</span><span>${escapeHTML(disk.model || "SSD interno")} • kernel ${escapeHTML(data.kernel || "—")} • ligado há ${escapeHTML(uptimeDays.toFixed(1))} dia(s)</span></div>`;
  const updated = el("pc-data-updated");
  if (updated) updated.textContent = `Atualizado em ${formatTime(data.checked_at || Date.now() / 1000)}`;
}

async function loadPCResources({openDialog = false, announce = false} = {}) {
  if (openDialog && selectors.pcDataDialog && !selectors.pcDataDialog.open) selectors.pcDataDialog.showModal();
  try {
    const data = await api("/api/control/pc-resources");
    renderPCResources(data);
    if (announce) toast("Dados atuais do PC carregados.", "success");
    return data;
  } catch (error) {
    if (openDialog && selectors.pcDataContent) selectors.pcDataContent.innerHTML = `<div class="inline-notice error">${escapeHTML(error.message)}</div>`;
    if (announce) toast(error.message, "error");
    return null;
  }
}

function formatQuotaDuration(minutes) {
  const value = Number(minutes || 0);
  if (!Number.isFinite(value) || value <= 0) return "janela não informada";
  if (value < 60) return `${Math.round(value)} min`;
  if (value < 1440) return `${Number.isInteger(value / 60) ? value / 60 : (value / 60).toFixed(1)} h`;
  const days = value / 1440;
  return `${Number.isInteger(days) ? days : days.toFixed(1)} dia${days === 1 ? "" : "s"}`;
}

function formatPlanName(value) {
  const plan = String(value || "").trim();
  if (!plan) return "Plano não informado";
  const known = {free:"Free", go:"Go", plus:"Plus", pro:"Pro", team:"Team", business:"Business", enterprise:"Enterprise", edu:"Edu"};
  return known[plan.toLowerCase()] || plan.replace(/(^|[_-])([a-z])/g, (_, lead, letter) => `${lead ? " " : ""}${letter.toUpperCase()}`);
}

function quotaRemainingPercent(windowData) {
  const used = Math.max(0, Math.min(100, Number(windowData?.usedPercent || 0)));
  return Math.max(0, 100 - used);
}

function weeklyQuotaWindow(limitsData) {
  const buckets = Object.values(limitsData?.rateLimitsByLimitId || {}).filter(Boolean);
  if (!buckets.length && limitsData?.rateLimits) buckets.push(limitsData.rateLimits);
  const windows = buckets.flatMap(bucket => [bucket.primary, bucket.secondary]).filter(Boolean);
  const weeklyMinutes = 7 * 24 * 60;
  return windows.find(windowData => Number(windowData.windowDurationMins) === weeklyMinutes)
    || windows.filter(windowData => Number(windowData.windowDurationMins) >= 6 * 24 * 60)
      .sort((left, right) => Math.abs(Number(left.windowDurationMins) - weeklyMinutes) - Math.abs(Number(right.windowDurationMins) - weeklyMinutes))[0]
    || buckets.find(bucket => bucket.secondary)?.secondary
    || windows.sort((left, right) => Number(right.windowDurationMins || 0) - Number(left.windowDurationMins || 0))[0]
    || null;
}

function quotaWindowHTML(label, windowData) {
  if (!windowData) return "";
  const used = Math.max(0, Math.min(100, Number(windowData.usedPercent || 0)));
  const remaining = quotaRemainingPercent(windowData);
  const reset = windowData.resetsAt ? formatTime(windowData.resetsAt) : "não informado";
  const severity = remaining <= 10 ? "danger" : remaining <= 30 ? "warning" : "healthy";
  return `<div class="quota-window ${severity}">
    <div class="quota-window-heading"><strong>${escapeHTML(label)}</strong><span>${remaining.toFixed(remaining % 1 ? 1 : 0)}% restante</span></div>
    <div class="quota-progress" role="progressbar" aria-label="${escapeHTML(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${remaining}"><span style="width:${remaining}%"></span></div>
    <div class="quota-window-meta"><span>${used.toFixed(used % 1 ? 1 : 0)}% usado</span><span>Janela de ${escapeHTML(formatQuotaDuration(windowData.windowDurationMins))}</span><span>Renova em ${escapeHTML(reset)}</span></div>
  </div>`;
}

function creditsSummaryHTML(credits) {
  if (credits == null) return '<span class="quota-muted">Saldo adicional não informado para esta conta.</span>';
  if (typeof credits === "number" || typeof credits === "string") return `<strong>${escapeHTML(credits)}</strong>`;
  if (Array.isArray(credits)) return `<strong>${credits.length}</strong><span> crédito(s) disponível(is)</span>`;
  const remaining = credits.remaining ?? credits.balance ?? credits.available ?? credits.amount ?? credits.value;
  const currency = credits.currency ? ` ${String(credits.currency).toUpperCase()}` : "";
  const unlimited = credits.unlimited === true || credits.isUnlimited === true;
  if (unlimited) return "<strong>Ilimitados</strong>";
  if (remaining != null) return `<strong>${escapeHTML(remaining)}${escapeHTML(currency)}</strong><span> disponíveis</span>`;
  return '<span class="quota-muted">Disponível, sem saldo numérico informado.</span>';
}

function codexUsageHTML(accountData, limitsData) {
  if (!accountData?.account) return '<article class="maintenance-card quota-card"><h3>Plano e cotas</h3><p>Entre na conta do Codex para consultar o uso.</p></article>';
  if (!limitsData) return '<article class="maintenance-card quota-card"><h3>Plano e cotas</h3><p>Os detalhes de uso ainda não foram retornados para esta conta.</p><span class="value">Atualize o painel para tentar novamente.</span></article>';
  const bucketsObject = limitsData.rateLimitsByLimitId || {};
  let buckets = Object.values(bucketsObject).filter(Boolean);
  if (!buckets.length && limitsData.rateLimits) buckets = [limitsData.rateLimits];
  const defaultBucket = buckets[0] || limitsData.rateLimits || {};
  const plan = accountData.account.planType || defaultBucket.planType || limitsData.planType;
  const windowRows = buckets.map((bucket, index) => {
    const name = bucket.limitName || (buckets.length > 1 ? bucket.limitId : "Uso do Codex") || `Cota ${index + 1}`;
    return `<section class="quota-bucket"><h4>${escapeHTML(name)}</h4>${quotaWindowHTML("Janela principal", bucket.primary)}${quotaWindowHTML("Janela secundária", bucket.secondary)}${bucket.rateLimitReachedType ? `<div class="quota-alert">Limite atingido: ${escapeHTML(bucket.rateLimitReachedType)}</div>` : ""}</section>`;
  }).join("");
  const resetCredits = limitsData.rateLimitResetCredits;
  const resetCount = resetCredits?.availableCount;
  const expirations = (resetCredits?.credits || []).map(item => item?.expiresAt).filter(Boolean).sort((a, b) => a - b);
  const resetExpiry = expirations.length ? ` • próximo vencimento ${formatTime(expirations[0])}` : "";
  return `<article class="maintenance-card quota-card">
    <div class="quota-card-heading"><div><h3>Plano e cotas do Codex</h3><p>Plano ChatGPT ${escapeHTML(formatPlanName(plan))}</p></div><button class="secondary-button" data-settings-action="refresh-codex-usage">Atualizar</button></div>
    <div class="quota-grid">${windowRows || '<span class="quota-muted">Nenhuma janela de uso foi informada.</span>'}</div>
    <div class="quota-extras"><div><span>Créditos adicionais</span>${creditsSummaryHTML(limitsData.credits ?? defaultBucket.credits)}</div><div><span>Redefinições de cota</span><strong>${resetCount == null ? "Não informado" : escapeHTML(resetCount)}</strong><small>${escapeHTML(resetExpiry.replace(/^ • /, ""))}</small></div></div>
    <p class="quota-footnote">As datas acima são de reposição das cotas. A data de cobrança ou renovação da assinatura não é fornecida pelo Codex App Server e deve ser consultada no gerenciamento do plano ChatGPT.</p>
  </article>`;
}

function renderHomeCodexUsage() {
  if (!selectors.homeCodexUsage) return;
  const workspace = workspaceGroup(activeWorkspace());
  const fallback = workspace === "system" ? "projects" : "system";
  const account = state.accounts[workspace] || state.accounts[fallback];
  const limits = state.rateLimits[workspace] || state.rateLimits[fallback];
  selectors.homeCodexUsage.innerHTML = codexUsageHTML(
    account,
    limits,
  );
  if (selectors.codexUsageSummary) {
    const weekly = weeklyQuotaWindow(limits);
    const remaining = weekly ? `${quotaRemainingPercent(weekly).toFixed(0)}% restante` : "detalhes";
    selectors.codexUsageSummary.textContent = account?.account ? remaining : "Entrar";
    const summary = selectors.codexUsageSummary.closest("summary");
    if (summary && weekly?.resetsAt) summary.title = `Cota semanal: ${remaining}. Renova em ${formatTime(weekly.resetsAt)}.`;
  }
}

function renderBackupHistory(history = []) {
  const card = el("backup-cloud-remote-path")?.closest(".maintenance-card");
  const actions = card?.querySelector(".card-actions");
  if (!card || !actions) return;
  card.querySelector(".backup-history")?.remove();
  const section = document.createElement("section");
  section.className = "backup-history";
  section.innerHTML = `<div class="backup-history-heading"><h4>Histórico dos backups</h4><small>Execuções automáticas e manuais</small></div>${history.length ? `<div class="backup-history-table-wrap"><table><thead><tr><th>Resultado</th><th>Horário</th><th>Duração</th><th>Tamanho</th></tr></thead><tbody>${history.slice(0, 30).map(item => { const status = String(item.status || "unknown"); const successful = status === "complete"; const label = successful ? "Sucesso" : status === "failed" ? "Erro" : status === "warning" ? "Atenção" : status === "running" ? "Em andamento" : status; const duration = Math.max(0, Number(item.duration_seconds || 0)); return `<tr><td><span class="backup-history-status ${successful ? "success" : status === "failed" ? "error" : "warn"}">${escapeHTML(label)}</span><small>${escapeHTML(item.message || "")}</small></td><td>Início: ${escapeHTML(formatTime(item.started_at) || "—")}<small>Fim: ${escapeHTML(formatTime(item.finished_at) || "—")}</small></td><td>${duration >= 60 ? `${Math.floor(duration / 60)}min ${Math.round(duration % 60)}s` : `${Math.round(duration)}s`}</td><td>${Number(item.size_bytes) > 0 ? formatBytes(item.size_bytes) : "—"}</td></tr>`; }).join("")}</tbody></table></div>` : '<div class="inline-notice">O histórico começará a ser preenchido na próxima execução do backup.</div>'}`;
  actions.before(section);
}

function humanStatus(ok, yes, no) {
  return ok ? yes : no;
}

function activeWorkspace() {
  if (!state.activeProject) return state.installMode === "projects" ? "projects" : "system";
  return state.activeProject.kind === "system" ? "system" : "projects";
}

function workspaceGroup(workspace) {
  return String(workspace || "system").startsWith("project:") ? "projects" : (workspace === "projects" ? "projects" : "system");
}

function activeEventWorkspace() {
  if (!state.activeProject && state.installMode === "projects") {
    const project = state.projects.find(item => item.kind !== "system");
    return project ? `project:${project.id}` : "projects";
  }
  if (!state.activeProject) return "system";
  return state.activeProject.kind === "system" ? "system" : `project:${state.activeProject.id}`;
}

function workspaceLabel(workspace = activeWorkspace()) {
  if (String(workspace).startsWith("project:")) {
    const projectId = String(workspace).split(":", 2)[1];
    const project = state.projects.find(item => item.id === projectId);
    return project ? `Projeto: ${project.name}` : "Projeto / Worker";
  }
  return workspace === "system" ? "Sistema / Control Plane" : "Projetos / Workers";
}

function activeBridge() {
  const workspace = activeWorkspace();
  return state.bridges[workspace] || state.bridges[workspaceGroup(workspace)] || {initialized:false, last_error:"Codex indisponível"};
}

function syncActiveBridgeUI() {
  const workspace = activeWorkspace();
  const bridge = state.bridges[workspace] || {initialized:false};
  state.bridgeReady = Boolean(bridge.initialized);
  if (state.bridgeReady) setStatus("ready", workspace === "system" ? "Sistema pronto" : "Projetos prontos");
  else setStatus("error", bridge.last_error || `${workspaceLabel(workspace)} indisponível`);
  return state.bridgeReady;
}

function activeAccount() {
  const workspace = activeWorkspace();
  return state.accounts[workspace] || state.accounts[workspaceGroup(workspace)] || null;
}

function workspaceQuery(workspace = activeWorkspace()) {
  const params = new URLSearchParams({workspace});
  if (workspace !== "system") {
    const project = state.activeProject?.kind !== "system" ? state.activeProject : state.projects.find(item => item.kind !== "system");
    if (project?.id) params.set("project_id", project.id);
  }
  return params.toString();
}

function makeToastDismissible(node, removeAfter = 5200) {
  let pointerId = null;
  let startX = 0;
  let startY = 0;
  let startedAt = 0;
  let offsetX = 0;
  let swiping = false;
  let removed = false;

  const removeTimer = setTimeout(() => dismiss(1), removeAfter);

  function dismiss(direction) {
    if (removed || !node.isConnected) return;
    removed = true;
    clearTimeout(removeTimer);
    node.classList.remove("dragging");
    node.classList.add("dismissed");
    const distance = node.getBoundingClientRect().width + 48;
    requestAnimationFrame(() => {
      node.style.transform = `translateX(${direction * distance}px)`;
      node.style.opacity = "0";
    });
    setTimeout(() => node.remove(), 220);
  }

  function restore() {
    node.classList.remove("dragging");
    requestAnimationFrame(() => {
      node.style.transform = "";
      node.style.opacity = "";
    });
  }

  node.addEventListener("pointerdown", event => {
    if (!event.isPrimary || event.button > 0 || removed) return;
    pointerId = event.pointerId;
    startX = event.clientX;
    startY = event.clientY;
    startedAt = performance.now();
    offsetX = 0;
    swiping = false;
    node.setPointerCapture?.(pointerId);
  });

  node.addEventListener("pointermove", event => {
    if (event.pointerId !== pointerId || removed) return;
    const deltaX = event.clientX - startX;
    const deltaY = event.clientY - startY;
    if (!swiping) {
      if (Math.abs(deltaX) < 8) return;
      if (Math.abs(deltaX) <= Math.abs(deltaY) * 1.15) return;
      swiping = true;
      node.classList.add("dragging");
    }
    event.preventDefault();
    offsetX = deltaX;
    const width = Math.max(node.getBoundingClientRect().width, 1);
    node.style.transform = `translateX(${deltaX}px)`;
    node.style.opacity = String(Math.max(.15, 1 - Math.abs(deltaX) / (width * .85)));
  });

  function finish(event, cancelled = false) {
    if (event.pointerId !== pointerId || removed) return;
    if (node.hasPointerCapture?.(pointerId)) node.releasePointerCapture(pointerId);
    pointerId = null;
    if (!swiping || cancelled) {
      restore();
      return;
    }
    const width = Math.max(node.getBoundingClientRect().width, 1);
    const elapsed = Math.max(performance.now() - startedAt, 1);
    const fastPush = Math.abs(offsetX) >= 28 && Math.abs(offsetX) / elapsed >= .45;
    const crossedThreshold = Math.abs(offsetX) >= Math.min(80, width * .25);
    if (fastPush || crossedThreshold) dismiss(Math.sign(offsetX) || 1);
    else restore();
  }

  node.addEventListener("pointerup", event => finish(event));
  node.addEventListener("pointercancel", event => finish(event, true));
}

function toast(message, type = "") {
  const node = document.createElement("div");
  node.className = `toast ${type}`.trim();
  node.textContent = message;
  node.title = "Deslize para o lado para dispensar";
  selectors.toastContainer.appendChild(node);
  makeToastDismissible(node);
}

// ---------------------------------------------------------------------------
// Device-bound authentication for remote browsers
// ---------------------------------------------------------------------------

const DEVICE_DB_NAME = "codex-linux-control-security";
const DEVICE_DB_VERSION = 1;
const DEVICE_STORE = "credentials";
const DEVICE_KEY = "current";

function base64UrlFromBytes(value) {
  const bytes = value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer || value);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function pairingTokenFromLocation() {
  const fragment = new URLSearchParams(location.hash.replace(/^#/, ""));
  return fragment.get("pair") || "";
}

function clearPairingFragment() {
  if (location.hash) history.replaceState(null, "", `${location.pathname}${location.search}`);
}

function browserDeviceName() {
  const ua = navigator.userAgent || "Navegador";
  let device = /Android/i.test(ua) ? "Android" : /iPad|iPhone|iPod/i.test(ua) ? "iPhone/iPad" : /Windows/i.test(ua) ? "Windows" : /Linux/i.test(ua) ? "Linux" : /Macintosh/i.test(ua) ? "Mac" : "Dispositivo";
  let browser = /Edg\//i.test(ua) ? "Edge" : /OPR\//i.test(ua) ? "Opera" : /Firefox\//i.test(ua) ? "Firefox" : /CriOS|Chrome\//i.test(ua) ? "Chrome" : /Safari\//i.test(ua) ? "Safari" : "Navegador";
  return `${device} • ${browser}`.slice(0, 100);
}

function openDeviceDB() {
  return new Promise((resolve, reject) => {
    if (!("indexedDB" in window)) return reject(new Error("Este navegador não oferece armazenamento seguro de chave local."));
    const request = indexedDB.open(DEVICE_DB_NAME, DEVICE_DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(DEVICE_STORE)) request.result.createObjectStore(DEVICE_STORE);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Não foi possível abrir o armazenamento seguro."));
  });
}

async function readDeviceCredential() {
  const db = await openDeviceDB();
  try {
    return await new Promise((resolve, reject) => {
      const transaction = db.transaction(DEVICE_STORE, "readonly");
      const request = transaction.objectStore(DEVICE_STORE).get(DEVICE_KEY);
      request.onsuccess = () => resolve(request.result || null);
      request.onerror = () => reject(request.error || new Error("Não foi possível ler a chave do dispositivo."));
    });
  } finally { db.close(); }
}

async function saveDeviceCredential(credential) {
  const db = await openDeviceDB();
  try {
    await new Promise((resolve, reject) => {
      const transaction = db.transaction(DEVICE_STORE, "readwrite");
      transaction.objectStore(DEVICE_STORE).put(credential, DEVICE_KEY);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error || new Error("Não foi possível salvar a chave do dispositivo."));
      transaction.onabort = () => reject(transaction.error || new Error("O armazenamento da chave foi cancelado."));
    });
  } finally { db.close(); }
}

async function removeDeviceCredential() {
  const db = await openDeviceDB();
  try {
    await new Promise((resolve, reject) => {
      const transaction = db.transaction(DEVICE_STORE, "readwrite");
      transaction.objectStore(DEVICE_STORE).delete(DEVICE_KEY);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error || new Error("Não foi possível remover a chave local."));
    });
  } finally { db.close(); }
}

async function generateDeviceCredential() {
  if (!window.isSecureContext || !crypto?.subtle) {
    throw new Error("O pareamento exige HTTPS privado ou acesso local seguro.");
  }
  const generated = await crypto.subtle.generateKey(
    {name:"ECDSA", namedCurve:"P-256"},
    true,
    ["sign", "verify"],
  );
  const publicJwk = await crypto.subtle.exportKey("jwk", generated.publicKey);
  const privateJwk = await crypto.subtle.exportKey("jwk", generated.privateKey);
  const privateKey = await crypto.subtle.importKey(
    "jwk",
    privateJwk,
    {name:"ECDSA", namedCurve:"P-256"},
    false,
    ["sign"],
  );
  return {deviceId:"", privateKey, publicJwk, name:browserDeviceName(), createdAt:Date.now()};
}

async function unauthenticatedJSON(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, {...options, headers, credentials:"same-origin"});
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = data && typeof data === "object" ? data.detail : data;
    const error = new Error(typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail || `HTTP ${response.status}`));
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return data;
}

function setDeviceAuthOverlay(visible, message = "") {
  selectors.deviceAuthOverlay?.classList.toggle("hidden", !visible);
  if (message && selectors.deviceAuthMessage) selectors.deviceAuthMessage.textContent = message;
}

function setDeviceAuthBusy(busy, message = "Verificando a chave do dispositivo…") {
  if (selectors.deviceAuthProgress) {
    selectors.deviceAuthProgress.textContent = message;
    selectors.deviceAuthProgress.classList.toggle("hidden", !busy);
  }
  const retry = el("retry-device-auth");
  if (retry) retry.disabled = busy;
}

async function registerPairedDevice(token) {
  setDeviceAuthOverlay(true, "Concluindo o pareamento criptográfico deste navegador…");
  setDeviceAuthBusy(true, "Criando uma chave privada exclusiva neste dispositivo…");
  const credential = await generateDeviceCredential();
  const result = await unauthenticatedJSON("/api/security/device/register", {
    method:"POST",
    body:JSON.stringify({token, public_jwk:credential.publicJwk, name:credential.name}),
  });
  credential.deviceId = result.device.id;
  await saveDeviceCredential(credential);
  clearPairingFragment();
  setDeviceAuthBusy(false);
  setDeviceAuthOverlay(false);
  return result.session;
}

async function enrollVerifiedBrowser(detail = {}) {
  const limit = Number(detail.device_limit || 6);
  setDeviceAuthOverlay(true, `Authenticator confirmado. Cadastrando este navegador (limite ${limit})…`);
  setDeviceAuthBusy(true, "Criando uma chave privada exclusiva neste navegador…");
  const credential = await generateDeviceCredential();
  const result = await unauthenticatedJSON("/api/security/device/enroll-verified", {
    method:"POST",
    body:JSON.stringify({public_jwk:credential.publicJwk, name:credential.name}),
  });
  if (result.pending) {
    credential.pendingRequestId = result.request.id;
    await saveDeviceCredential(credential);
    setDeviceAuthBusy(false);
    setDeviceAuthOverlay(true, result.message || "Solicitação pendente de aprovação na rede local.");
    return null;
  }
  credential.deviceId = result.device.id;
  await saveDeviceCredential(credential);
  setDeviceAuthBusy(false);
  setDeviceAuthOverlay(false);
  return result.session;
}

async function resumePendingEnrollment(credential) {
  if (!credential?.pendingRequestId) return null;
  setDeviceAuthOverlay(true, "Verificando a solicitação de cadastro deste dispositivo…");
  setDeviceAuthBusy(true);
  const result = await unauthenticatedJSON(`/api/security/device/enrollment/${encodeURIComponent(credential.pendingRequestId)}`);
  if (result.session && result.device) {
    credential.deviceId = result.device.id;
    delete credential.pendingRequestId;
    await saveDeviceCredential(credential);
    setDeviceAuthBusy(false);
    setDeviceAuthOverlay(false);
    return result.session;
  }
  if (["rejected", "expired"].includes(result.request?.status)) {
    await removeDeviceCredential();
    throw new Error(result.request.status === "rejected" ? "A solicitação foi recusada na rede local." : "A solicitação expirou; tente novamente.");
  }
  setDeviceAuthBusy(false);
  setDeviceAuthOverlay(true, "Solicitação pendente. Aprove-a em Configurações → Segurança usando um dispositivo na rede local.");
  return null;
}

async function authenticatePairedDevice(credential) {
  if (!credential?.deviceId || !credential?.privateKey) return null;
  if (!window.isSecureContext || !crypto?.subtle) throw new Error("A autenticação do dispositivo exige uma conexão HTTPS segura.");
  setDeviceAuthOverlay(true, "Validando a chave criptográfica deste dispositivo…");
  setDeviceAuthBusy(true);
  const challenge = await unauthenticatedJSON(`/api/security/device/challenge?device_id=${encodeURIComponent(credential.deviceId)}`);
  const signature = await crypto.subtle.sign(
    {name:"ECDSA", hash:"SHA-256"},
    credential.privateKey,
    new TextEncoder().encode(challenge.payload),
  );
  const result = await unauthenticatedJSON("/api/security/device/authenticate", {
    method:"POST",
    body:JSON.stringify({
      device_id:credential.deviceId,
      challenge_id:challenge.challenge_id,
      signature:base64UrlFromBytes(signature),
    }),
  });
  setDeviceAuthBusy(false);
  setDeviceAuthOverlay(false);
  return result.session;
}

async function beginMicrosoftAuthentication(session, stepUp = false) {
  if (!session?.csrf) throw new Error("Sessão local indisponível para iniciar o Microsoft Authenticator.");
  const response = await fetch(stepUp ? "/api/auth/entra/step-up" : "/api/auth/entra/start", {
    method:"POST",
    headers:{"X-CLC-CSRF":session.csrf},
    credentials:"same-origin",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data?.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message || "Não foi possível abrir o Microsoft Authenticator.");
  }
  sessionStorage.setItem("clc-entra-return", stepUp ? "step-up" : "login");
  window.location.assign(data.url);
}

function processEntraReturn() {
  const params = new URLSearchParams(location.search);
  const result = params.get("entra");
  if (!result) return;
  const clean = `${location.pathname}${location.hash && !location.hash.startsWith("#entra-") ? location.hash : ""}`;
  history.replaceState(null, "", clean || "/");
  if (result === "ok") toast("Identidade confirmada no Microsoft Authenticator.", "success");
  else toast(params.get("message") || "A autenticação Microsoft não foi concluída.", "error");
  sessionStorage.removeItem("clc-entra-return");
}

async function establishSession() {
  const response = await fetch("/api/session", {credentials:"same-origin"});
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (response.ok) {
    setDeviceAuthOverlay(false);
    return data;
  }
  const detail = data && typeof data === "object" ? data.detail : null;
  if (response.status !== 401 || detail?.code !== "device_auth_required") {
    throw new Error(typeof detail === "string" ? detail : detail?.message || "Acesso negado");
  }

  const token = pairingTokenFromLocation();
  try {
    if (token) return await registerPairedDevice(token);
    const credential = await readDeviceCredential();
    if (credential) {
      if (credential.pendingRequestId) return await resumePendingEnrollment(credential);
      try {
        return await authenticatePairedDevice(credential);
      } catch (error) {
        if (error.status === 400 || error.status === 403 || /revog|inválid|expir/i.test(error.message)) {
          await removeDeviceCredential().catch(() => null);
          if (detail.verified_enrollment_available) return await enrollVerifiedBrowser(detail);
        }
        throw error;
      }
    }
    if (detail.verified_enrollment_available) return await enrollVerifiedBrowser(detail);
  } catch (error) {
    console.warn("Autenticação vinculada ao dispositivo falhou", error);
    if (error.status === 400 || error.status === 403 || /revog|inválid|expir/i.test(error.message)) {
      await removeDeviceCredential().catch(() => null);
    }
    setDeviceAuthBusy(false);
    setDeviceAuthOverlay(true, error.message);
    return null;
  }

  setDeviceAuthBusy(false);
  setDeviceAuthOverlay(true, detail.message || "Confirme sua identidade pelo Authenticator para cadastrar este navegador.");
  return null;
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData) && !(options.body instanceof Blob)) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && state.csrf) headers.set("X-CLC-CSRF", state.csrf);
  const response = await fetch(path, {...options, method, headers, credentials:"same-origin"});
  let data = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) data = await response.json();
  else data = await response.text();

  const isAuthEndpoint = path.startsWith("/api/security/device/") || path.startsWith("/api/auth/entra/") || path === "/api/session";
  const detail = data && typeof data === "object" ? data.detail : data;
  if (response.status === 401 && detail?.code === "entra_auth_required" && !isAuthEndpoint) {
    const session = state.session || await establishSession();
    await beginMicrosoftAuthentication(session, false);
    throw new Error("Redirecionando para o Microsoft Authenticator…");
  }
  if (response.status === 428 && detail?.code === "entra_step_up_required" && !isAuthEndpoint) {
    const session = state.session || await establishSession();
    await beginMicrosoftAuthentication(session, true);
    throw new Error("Confirmação forte solicitada no Microsoft Authenticator.");
  }
  if (response.status === 401 && !isAuthEndpoint && !options._retried) {
    await renewSession();
    return api(path, {...options, _retried:true});
  }
  if (!response.ok) {
    throw new Error(typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail || `HTTP ${response.status}`));
  }
  return data;
}

let sessionRenewalPromise = null;

async function renewSession() {
  // A service restart invalidates the in-memory HTTP session. Several API
  // requests and the events WebSocket can notice that at the same time. Keep
  // one device challenge in flight so the cookie and CSRF token always belong
  // to the same newly-created session.
  if (!sessionRenewalPromise) {
    sessionRenewalPromise = (async () => {
      const session = await establishSession();
      if (!session) throw new Error("A sessão expirou e este dispositivo precisa ser autorizado novamente");
      state.session = session;
      state.csrf = session.csrf;
      state.identity = session.identity;
      return session;
    })();
  }
  try {
    return await sessionRenewalPromise;
  } finally {
    sessionRenewalPromise = null;
  }
}

async function bootstrap() {
  try {
    const session = await establishSession();
    if (!session) return;
    state.session = session;
    state.csrf = session.csrf;
    state.identity = session.identity;
    selectors.connectionLabel.textContent = session.identity === "localhost" ? "Acesso local" : session.identity;
    processEntraReturn();
    if (session.entra?.required && !session.entra?.verified) {
      await beginMicrosoftAuthentication(session, false);
      return;
    }
    connectEvents();
    registerServiceWorker();

    if (!session.setup_completed && session.is_local) {
      await enterSetupWizard();
      return;
    }
    await initializeMainInterface();
  } catch (error) {
    setStatus("error", "Acesso negado");
    selectors.empty.innerHTML = `<h1>Não foi possível abrir o aplicativo</h1><p>${escapeHTML(error.message)}</p>`;
    console.error(error);
  }
}

async function initializeMainInterface() {
  selectors.setupOverlay.classList.add("hidden");
  state.setup.active = false;
  if (state.projects.length) {
    renderProjects();
    publishCachedThreadSummaries();
    setConversationContextUI();
  }
  await loadStatus();
  await loadProjects({loadConversationList:false});
  void refreshUpdateControl();
  clearInterval(state.systemUpdateAutomaticTimer);
  state.systemUpdateAutomaticTimer = window.setInterval(() => {
    if (document.visibilityState === "visible") void maybeStartAutomaticSystemUpdate();
  }, 5000);
  await loadPCResources();
  clearInterval(state.pcResourceTimer);
  state.pcResourceTimer = window.setInterval(() => {
    if (document.visibilityState === "visible") void loadPCResources();
  }, 60000);
  state.mainInterfaceReady = true;
  await openNotificationTarget();
  if (state.bridgeReady) {
    void Promise.all([loadModels(), loadAccount()]);
    if (!state.activeProject) void loadThreads();
  } else selectors.loginBanner.classList.add("hidden");
}

async function refreshUpdateControl() {
  const button = el("system-update");
  if (!button) return null;
  let rollout = null;
  try {
    rollout = await api("/api/update/status");
    const localPreference = localStorage.getItem(SYSTEM_UPDATE_AUTOMATIC_KEY);
    if (localPreference !== null && Boolean(rollout.automatic) !== (localPreference !== "false")) {
      const updated = await api("/api/update/automatic", {
        method:"POST",
        body:JSON.stringify({enabled:localPreference !== "false"}),
      });
      rollout = {...rollout, ...updated};
    }
    localStorage.removeItem(SYSTEM_UPDATE_AUTOMATIC_KEY);
  } catch (_) {
    try {
      const response = await fetch(`/release-status.json?v=${Date.now()}`, {cache:"no-store"});
      if (response.ok) {
        const fallback = await response.json();
        rollout = {...fallback, source:"static", activation_mode:fallback.activation_mode || "initial-maintenance"};
      }
    } catch (_) {}
  }
  try {
    rollout ||= {available:false, pending:false, state:"unavailable"};
    const pending = Boolean(rollout.available && rollout.pending);
    const pendingCount = Math.max(0, Number(rollout.pending_count || (pending ? 1 : 0)));
    const blockers = Array.isArray(rollout.blocking_conversations) ? rollout.blocking_conversations : [];
    state.systemUpdate = rollout;
    state.systemUpdateAutomatic = rollout.source === "static" && rollout.activation_mode !== "rolling"
      ? localStorage.getItem(SYSTEM_UPDATE_AUTOMATIC_KEY) !== "false"
      : rollout.automatic !== false;
    button.dataset.updatePending = pending ? "true" : "false";
    button.dataset.activationMode = rollout.activation_mode || (rollout.source === "static" ? "initial-maintenance" : "rolling");
    button.classList.toggle("update-ready", pending);
    button.classList.toggle("update-blocked", pending && blockers.length > 0);
    button.dataset.blockerCount = blockers.length ? String(blockers.length) : "";
    button.dataset.pendingCount = pending ? (pendingCount > 99 ? "99+" : String(pendingCount)) : "";
    const pendingLabel = `${pendingCount} ${pendingCount === 1 ? "atualização" : "atualizações"}`;
    button.title = pending
      ? blockers.length
        ? `${blockers.length} conversa${blockers.length === 1 ? " precisa" : "s precisam"} terminar antes de aplicar ${pendingLabel}`
        : `Aplicar ${pendingLabel} em sequência`
      : rollout.available ? "Sistema atualizado" : "Atualização do sistema indisponível";
    button.setAttribute("aria-label", button.title);
    button.disabled = !pending;
    const automaticToggle = el("settings-system-update-automatic");
    if (automaticToggle) automaticToggle.checked = state.systemUpdateAutomatic;
    if (pending) window.setTimeout(() => void maybeStartAutomaticSystemUpdate(), 1000);
    return rollout;
  } catch (_) {
    button.dataset.updatePending = "false";
    button.dataset.pendingCount = "";
    button.disabled = true;
    return null;
  }
}

function hasKnownActiveDexTurns() {
  if (state.activeTurnId || state.turnSubmissionPending) return true;
  for (const threads of state.projectThreads.values()) {
    if (threads.some(thread => threadStatus(thread) === "active")) return true;
  }
  return false;
}

async function maybeStartAutomaticSystemUpdate() {
  if (!state.systemUpdateAutomatic || state.systemUpdateStarting) return;
  const button = el("system-update");
  if (!button || button.dataset.updatePending !== "true" || hasKnownActiveDexTurns()) return;
  state.systemUpdateStarting = true;
  try {
    if (button.dataset.activationMode === "initial-maintenance") {
      // Preserve the one-time migration path for installations that have not
      // reached the rolling coordinator yet.
      const response = await fetch(`/api/health?update-check=${Date.now()}`, {cache:"no-store"});
      if (!response.ok) return;
      const health = await response.json();
      if (Number(health.active_turns || 0) > 0 || hasKnownActiveDexTurns()) return;
      await handleSystemUpdate({automatic:true});
      return;
    }

    // The rolling coordinator starts automatic releases itself. While it is
    // waiting for active turns, keep the Dex fully usable and do not present a
    // progress dialog: no implementation work has begun yet.
    const previousPendingRelease = String(state.systemUpdate?.pending_release || "");
    const rollout = await api("/api/update/status");
    state.systemUpdate = rollout;
    if (!rollout.pending) {
      button.dataset.updatePending = "false";
      button.classList.remove("update-ready");
      button.disabled = true;
      if (rollout.state === "ready" && previousPendingRelease && rollout.active_release === previousPendingRelease) {
        window.location.reload();
      }
      return;
    }

    const blockingPhases = new Set(["switching", "restarting", "health-check"]);
    if (rollout.state !== "activating" || !blockingPhases.has(String(rollout.phase || ""))) return;
    state.systemUpdateProgressDismissed = false;
    el("system-update-progress-close").disabled = false;
    showSystemUpdateProgress(rollout.percent, rollout.message || "Atualizando o sistema…");
    void monitorRollingSystemUpdate();
  } catch (_) {
    // A failed status check must leave the automatic update silent and let the
    // coordinator retry without interrupting the operator.
  } finally {
    window.setTimeout(() => { state.systemUpdateStarting = false; }, 5000);
  }
}

async function setAutomaticSystemUpdates(enabled) {
  state.systemUpdateAutomatic = Boolean(enabled);
  localStorage.setItem(SYSTEM_UPDATE_AUTOMATIC_KEY, enabled ? "true" : "false");
  try {
    const result = await api("/api/update/automatic", {
      method:"POST",
      body:JSON.stringify({enabled:Boolean(enabled)}),
    });
    state.systemUpdateAutomatic = result.automatic !== false;
    localStorage.removeItem(SYSTEM_UPDATE_AUTOMATIC_KEY);
  } catch (error) {
    if (state.systemUpdate?.source !== "static") throw error;
  }
  toast(enabled
    ? "Atualizações automáticas ativadas. Releases serão aplicadas quando todas as conversas terminarem."
    : "Atualizações automáticas desativadas. Use o botão no cabeçalho para aplicar releases.", "success");
  if (enabled) void maybeStartAutomaticSystemUpdate();
}

async function waitForUpdatedBackend() {
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    await new Promise(resolve => window.setTimeout(resolve, 1000));
    try {
      const response = await fetch(`/api/health?rollout=${Date.now()}`, {cache:"no-store"});
      if (response.ok) {
        window.location.reload();
        return;
      }
    } catch (_) {}
  }
  toast("A atualização continua em andamento. Tente atualizar a página em instantes.", "error");
  const button = el("system-update");
  if (button) button.disabled = false;
}

function showSystemUpdateProgress(percent = 2, message = "Preparando a atualização do sistema…") {
  const dialog = el("system-update-dialog");
  if (!dialog.open && !state.systemUpdateProgressDismissed) dialog.showModal();
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  el("system-update-progress").setAttribute("aria-valuenow", String(value));
  el("system-update-progress-fill").style.width = `${value}%`;
  el("system-update-progress-percent").textContent = `${Math.round(value)}%`;
  el("system-update-progress-message").textContent = message;
}

function blockingConversationTitle(blocker) {
  const threadId = String(blocker?.thread_id || "");
  const candidates = [state.threads, ...state.projectThreads.values()];
  for (const threads of candidates) {
    const thread = (threads || []).find(item => item.id === threadId);
    if (thread) return conversationTitle(thread);
  }
  return String(blocker?.title || `Conversa ${threadId.slice(-8)}`);
}

function showSystemUpdateBlockers(blockers) {
  const dialog = el("system-update-blockers-dialog");
  const list = el("system-update-blockers-list");
  const items = Array.isArray(blockers) ? blockers : [];
  const pendingCount = Math.max(1, Number(state.systemUpdate?.pending_count || 1));
  const updateLabel = `${pendingCount} ${pendingCount === 1 ? "atualização pendente" : "atualizações pendentes"}`;
  el("system-update-blockers-summary").textContent = items.length === 1
    ? `Esta conversa precisa terminar antes que ${updateLabel} possa${pendingCount === 1 ? "" : "m"} começar.`
    : `${items.length} conversas precisam terminar antes que ${updateLabel} possa${pendingCount === 1 ? "" : "m"} começar.`;
  list.innerHTML = items.map((blocker, index) => `
    <button class="system-update-blocker" type="button" data-blocker-index="${index}">
      <span class="project-badge">${escapeHTML(blocker.project_name || "Projeto")}</span>
      <strong>${escapeHTML(blockingConversationTitle(blocker))}</strong>
      <small>${blocker.waiting_for_operator ? "Aguardando ação do operador" : "Em execução"}</small>
    </button>
  `).join("");
  list.querySelectorAll("[data-blocker-index]").forEach(button => {
    button.addEventListener("click", async () => {
      const blocker = items[Number(button.dataset.blockerIndex)];
      if (!blocker?.thread_id) return;
      dialog.close();
      if (blocker.project_id && state.activeProject?.id !== blocker.project_id) await selectProject(blocker.project_id);
      await openThread(blocker.thread_id);
    });
  });
  if (!dialog.open) dialog.showModal();
}

async function monitorInitialSystemUpdate() {
  const deadline = Date.now() + 6 * 60 * 1000;
  let observedRunning = false;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`/release-progress.json?v=${Date.now()}`, {cache:"no-store"});
      if (response.ok) {
        const progress = await response.json();
        observedRunning ||= progress.state === "running";
        showSystemUpdateProgress(progress.percent, progress.message || "Atualizando o sistema…");
        if (progress.state === "complete") {
          el("system-update-progress-close").disabled = false;
          window.setTimeout(() => window.location.reload(), 1200);
          return;
        }
        if (["failed", "rolled-back"].includes(progress.state)) {
          el("system-update-progress-close").disabled = false;
          return;
        }
      }
    } catch (_) {
      if (observedRunning) showSystemUpdateProgress(55, "O Dex está reiniciando; aguardando reconexão…");
    }
    await new Promise(resolve => window.setTimeout(resolve, 1000));
  }
  showSystemUpdateProgress(100, "A atualização demorou mais que o esperado. Verifique o estado do sistema.");
  el("system-update-progress-close").disabled = false;
}

async function monitorRollingSystemUpdate() {
  const deadline = Date.now() + 6 * 60 * 1000;
  while (Date.now() < deadline) {
    try {
      const rollout = await api("/api/update/status");
      showSystemUpdateProgress(rollout.percent, rollout.message || rollout.error || "Atualizando o sistema…");
      if (["ready", "rolled_back", "failed"].includes(rollout.state) && Number(rollout.percent) >= 100) {
        el("system-update-progress-close").disabled = false;
        if (rollout.state === "ready") window.setTimeout(() => window.location.reload(), 400);
        return;
      }
    } catch (_) {
      showSystemUpdateProgress(60, "O backend está reiniciando; aguardando reconexão…");
    }
    await new Promise(resolve => window.setTimeout(resolve, 250));
  }
  el("system-update-progress-close").disabled = false;
}

async function handleSystemUpdate({automatic = false} = {}) {
  const button = el("system-update");
  if (button?.dataset.updatePending !== "true") return;
  if (!automatic && button.dataset.activationMode !== "initial-maintenance") {
    if (state.systemUpdate?.source === "static") {
      const blockers = Array.isArray(state.systemUpdate.blocking_conversations)
        ? state.systemUpdate.blocking_conversations
        : [];
      if (blockers.length) showSystemUpdateBlockers(blockers);
      else toast(state.systemUpdate.message || "A atualização está na fila do Control Plane.", "success");
      return;
    }
    try {
      const rollout = await api("/api/update/status");
      state.systemUpdate = rollout;
      const blockers = Array.isArray(rollout.blocking_conversations) ? rollout.blocking_conversations : [];
      if (blockers.length) {
        showSystemUpdateBlockers(blockers);
        return;
      }
    } catch (error) {
      toast(error.message, "error");
      return;
    }
  }
  if (automatic) {
    // Recheck immediately before touching the dialog. This closes the gap
    // between the periodic eligibility check and the actual UI transition.
    // On any uncertainty, leave the release pending and keep the Dex usable.
    try {
      const response = await fetch(`/api/health?update-open=${Date.now()}`, {cache:"no-store"});
      if (!response.ok) return;
      const health = await response.json();
      if (Number(health.active_turns || 0) > 0 || hasKnownActiveDexTurns()) return;
    } catch (_) {
      return;
    }
  }
  state.systemUpdateProgressDismissed = false;
  el("system-update-progress-close").disabled = false;
  if (button.dataset.activationMode === "initial-maintenance") {
    button.disabled = true;
    showSystemUpdateProgress(3, automatic ? "Conversas concluídas; iniciando a atualização automática…" : "Solicitando a atualização ao Control Plane…");
    void monitorInitialSystemUpdate();
    try {
      await api("/api/control/action", {
        method:"POST",
        body:JSON.stringify({
          action:"service",
          params:{unit:"sasocq-dex-initial-rollout.service", operation:"start"},
        }),
      });
    } catch (_) {
      // The expected backend transition can close this request before a response.
      showSystemUpdateProgress(15, "Atualização iniciada; aguardando a troca do backend…");
    }
    return;
  }
  button.disabled = true;
  showSystemUpdateProgress(4, automatic ? "Atualização automática preparada; confirmando que todos os turnos terminaram…" : "Aguardando os turnos ativos terminarem…");
  try {
    const result = await api("/api/update/activate", {method:"POST"});
    toast(result.message || "Atualizações preparadas. Todas serão aplicadas em sequência assim que os turnos terminarem.", "success");
    void monitorRollingSystemUpdate();
  } catch (error) {
    button.disabled = false;
    toast(error.message, "error");
    await refreshUpdateControl();
  }
}

async function openNotificationTarget(payload = null) {
  const query = new URLSearchParams(location.search);
  const requested = payload && typeof payload === "object" ? payload : state.pendingNotificationTarget || {};
  const targetUrl = new URL(String(requested.url || location.href), location.origin);
  const projectId = String(requested.projectId || targetUrl.searchParams.get("project") || query.get("project") || "");
  const threadId = String(requested.threadId || targetUrl.searchParams.get("thread") || query.get("thread") || "");
  if (!projectId || !threadId) return false;
  state.pendingNotificationTarget = {projectId, threadId, url:targetUrl.href};
  if (!state.mainInterfaceReady) return false;
  if (!state.projects.some(project => project.id === projectId)) return false;
  if (state.activeProject?.id !== projectId) await selectProject(projectId);
  if (state.activeProject?.id !== projectId) return false;
  await openThread(threadId);
  state.pendingNotificationTarget = null;
  query.delete("project");
  query.delete("thread");
  const clean = `${location.pathname}${query.size ? `?${query}` : ""}${location.hash}`;
  history.replaceState(null, "", clean);
  return true;
}

async function loadStatus() {
  const data = await api("/api/status");
  state.lastStatus = data;
  state.installMode = data.app?.install_mode === "projects" ? "projects" : "full";
  document.body.dataset.installMode = state.installMode;
  const legacy = data.bridge || {};
  state.bridges = {
    system: {...(data.bridges?.system || legacy)},
    projects: {...(data.bridges?.projects || {})},
  };
  syncActiveBridgeUI();
  if (state.identity === "localhost") await loadPairedDevices();
  renderSettings(data);
  void refreshRemoteStatus().catch(() => null);
  return data;
}

async function loadProjects({loadConversationList = true} = {}) {
  const data = await api("/api/projects");
  const seen = new Set();
  state.projects = (data.projects || []).filter(project => {
    const key = `${project.kind || "project"}:${project.id}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  try { localStorage.setItem(PROJECT_CATALOG_CACHE_KEY, JSON.stringify(state.projects)); }
  catch { /* The live catalog remains available when browser storage is full. */ }
  if (state.activeProject) state.activeProject = state.projects.find(item => item.id === state.activeProject.id) || null;
  if (!state.activeProject && state.installMode === "projects") {
    state.activeProject = state.projects.find(item => item.kind !== "system") || null;
  }
  renderProjects();
  setConversationContextUI();
  if (loadConversationList) await loadThreads();
  return state.projects;
}

async function loadModels(workspace = activeWorkspace()) {
  const preferenceContext = composerPreferenceContextKey(state.activeProject);
  try {
    const data = await api(`/api/models?${workspaceQuery(workspace)}`);
    if (workspace !== activeWorkspace() || preferenceContext !== composerPreferenceContextKey(state.activeProject)) return data;
    state.models = (data.data || []).filter(model => !model.hidden);
    selectors.model.innerHTML = "";
    state.models.forEach(model => {
      const option = document.createElement("option");
      option.value = model.id || model.model;
      option.textContent = model.displayName || model.model || model.id;
      option.title = model.description || option.textContent;
      selectors.model.appendChild(option);
    });
    if (!state.models.length) {
      selectors.model.innerHTML = '<option value="">Modelo indisponível</option>';
      selectors.model.disabled = true;
      syncModelDependentControls();
      return data;
    }
    selectors.model.disabled = false;
    const preferred = state.models.find(model => (model.id || model.model) === state.composerPreferences.model);
    const selected = preferred || state.models.find(model => model.isDefault) || state.models[0];
    selectors.model.value = selected.id || selected.model;
    state.composerPreferences.model = selectors.model.value;
    syncModelDependentControls();
    saveComposerPreferences();
    return data;
  } catch (error) {
    addActivity(`Modelos • ${workspaceLabel(workspace)}`, error.message, "error");
    return null;
  }
}

function composerPreferencesForProject(project) {
  const key = composerPreferenceContextKey(project);
  const codex = project?.kind === "system" ? "system" : "projects";
  const local = normalizeComposerPreferences(state.composerPreferenceStore.contexts[key], state.composerPreferenceStore.networkAccess);
  const remoteSource = project?.clc?.composer_preferences;
  const remote = !remoteSource?.codex || remoteSource.codex === codex
    ? normalizeComposerPreferences(remoteSource, state.composerPreferenceStore.networkAccess)
    : emptyComposerPreferences(state.composerPreferenceStore.networkAccess);
  const hasLocal = Boolean(local.model || local.effort || local.serviceTier || local.updatedAt);
  const hasRemote = Boolean(remote.model || remote.effort || remote.serviceTier || remote.updatedAt);
  let selected = hasLocal && (!hasRemote || local.updatedAt > remote.updatedAt) ? local : remote;
  if (!hasLocal && !hasRemote && state.composerPreferenceStore.legacy) {
    selected = {...state.composerPreferenceStore.legacy, updatedAt:Date.now()};
    state.composerPreferenceStore.legacy = null;
    try { localStorage.removeItem(LEGACY_COMPOSER_PREFERENCES_STORAGE_KEY); }
    catch { /* A migração permanece válida para esta página. */ }
  }
  if (!key) return {...selected, networkAccess:state.composerPreferenceStore.networkAccess};
  state.composerPreferenceStore.contexts[key] = selected;
  try {
    localStorage.setItem(COMPOSER_PREFERENCES_STORAGE_KEY, JSON.stringify({
      contexts:state.composerPreferenceStore.contexts,
      networkAccess:state.composerPreferenceStore.networkAccess,
    }));
  } catch { /* O metadado remoto continua sendo a fonte durável. */ }
  return {...selected, networkAccess:state.composerPreferenceStore.networkAccess};
}

function composerPreferenceRecord(project, preferences) {
  return {
    codex: project?.kind === "system" ? "system" : "projects",
    model: preferences.model,
    effort: preferences.effort,
    service_tier: preferences.serviceTier,
    updated_at: preferences.updatedAt,
  };
}

function syncComposerPreferences(project, preferences) {
  if (!project?.id) return;
  const key = composerPreferenceContextKey(project);
  const record = composerPreferenceRecord(project, preferences);
  const previous = state.composerPreferenceSyncs.get(key) || Promise.resolve();
  const current = previous.catch(() => null).then(async () => {
    const data = await api(`/api/projects/${encodeURIComponent(project.id)}/metadata`, {
      method:"PATCH",
      body:JSON.stringify({composer_preferences:record}),
    });
    const liveProject = state.projects.find(item => item.id === project.id);
    if (liveProject) liveProject.clc = {...(liveProject.clc || {}), composer_preferences:data.metadata?.composer_preferences || record};
  }).catch(error => addActivity(`Preferências • ${project.name}`, error.message, "error"));
  state.composerPreferenceSyncs.set(key, current);
  void current.finally(() => {
    if (state.composerPreferenceSyncs.get(key) === current) state.composerPreferenceSyncs.delete(key);
  });
}

function saveComposerPreferences() {
  const updatedAt = Date.now();
  state.composerPreferences = {
    model: selectors.model?.value || state.composerPreferences.model || "",
    effort: selectors.effort?.value || "",
    serviceTier: selectors.speed?.value || "",
    networkAccess: selectors.network?.value === "restricted" ? "restricted" : "enabled",
    updatedAt,
  };
  const project = state.activeProject;
  const key = composerPreferenceContextKey(project);
  if (key) state.composerPreferenceStore.contexts[key] = state.composerPreferences;
  state.composerPreferenceStore.networkAccess = state.composerPreferences.networkAccess;
  state.composerPreferenceStore.legacy = null;
  try {
    localStorage.setItem(COMPOSER_PREFERENCES_STORAGE_KEY, JSON.stringify({
      contexts:state.composerPreferenceStore.contexts,
      networkAccess:state.composerPreferenceStore.networkAccess,
    }));
    localStorage.removeItem(LEGACY_COMPOSER_PREFERENCES_STORAGE_KEY);
  }
  catch { /* The controls still work for the current page when storage is unavailable. */ }
  syncComposerPreferences(project, state.composerPreferences);
}

function effortName(value) {
  return ({minimal:"Mínimo", low:"Baixo", medium:"Médio", high:"Alto", xhigh:"Extra alto", ultra:"Ultra"})[value] || value;
}

function syncModelDependentControls() {
  const model = state.models.find(item => (item.id || item.model) === selectors.model.value);
  const efforts = (model?.supportedReasoningEfforts || []).map(item => typeof item === "string"
    ? {reasoningEffort:item, description:""}
    : item).filter(item => item.reasoningEffort);
  const fallbackEfforts = ["low", "medium", "high", "xhigh"].map(reasoningEffort => ({reasoningEffort, description:""}));
  const availableEfforts = efforts.length ? efforts : fallbackEfforts;
  selectors.effort.innerHTML = "";
  availableEfforts.forEach(item => {
    const option = document.createElement("option");
    option.value = item.reasoningEffort;
    option.textContent = `Raciocínio: ${effortName(item.reasoningEffort)}`;
    option.title = item.description || option.textContent;
    selectors.effort.appendChild(option);
  });
  const preferredEffort = availableEfforts.some(item => item.reasoningEffort === state.composerPreferences.effort)
    ? state.composerPreferences.effort
    : model?.defaultReasoningEffort || availableEfforts[0]?.reasoningEffort || "";
  selectors.effort.value = preferredEffort;
  selectors.effort.disabled = !availableEfforts.length;

  const tiers = (model?.serviceTiers || []).filter(tier => tier?.id);
  selectors.speed.innerHTML = "";
  const defaultTierOption = document.createElement("option");
  defaultTierOption.value = "__default__";
  defaultTierOption.textContent = "Velocidade: Padrão";
  defaultTierOption.title = "Usa o processamento padrão disponível para o modelo e a conta.";
  selectors.speed.appendChild(defaultTierOption);
  if (!tiers.length) {
    selectors.speed.disabled = true;
  } else {
    tiers.forEach(tier => {
      const option = document.createElement("option");
      option.value = tier.id;
      option.textContent = `Velocidade: ${tier.name || tier.id}`;
      option.title = tier.description || option.textContent;
      selectors.speed.appendChild(option);
    });
    const preferredTier = state.composerPreferences.serviceTier === "__default__"
      ? "__default__"
      : tiers.some(tier => tier.id === state.composerPreferences.serviceTier)
        ? state.composerPreferences.serviceTier
        : model?.defaultServiceTier || tiers[0].id;
    selectors.speed.value = preferredTier;
    selectors.speed.disabled = false;
  }
  selectors.network.value = state.composerPreferences.networkAccess === "restricted" ? "restricted" : "enabled";
}

async function loadAccount(options = {}, workspace = activeWorkspace()) {
  try {
    const data = await api(`/api/account?${workspaceQuery(workspace)}`);
    state.accounts[workspace] = data;
    if (String(workspace).startsWith("project:")) state.accounts.projects = data;
    if (data.account) await loadRateLimits(workspace, {announce:false, render:false});
    else state.rateLimits[workspaceGroup(workspace)] = null;
    renderHomeCodexUsage();
    updateMobileNavigationAuth();
    if (workspace === activeWorkspace()) {
      const needsLogin = Boolean(data.requiresOpenaiAuth && !data.account);
      selectors.loginBanner.classList.toggle("hidden", !needsLogin || state.setup.active);
    }
    if (state.setup.active && !options.noRender) renderSetupWizard();
    else if (!options.noRender && state.lastStatus) renderSettings(state.lastStatus);
    return data;
  } catch (error) {
    // A timeout or a temporary bridge restart is not evidence that the saved
    // account disappeared. Keep the last authoritative state and never turn a
    // transport failure into a new login request.
    updateMobileNavigationAuth();
    addActivity(`Conta • ${workspaceLabel(workspace)}`, error.message, "error");
    return null;
  }
}

async function loadConfiguredAccounts() {
  if (state.installMode === "projects") {
    return Promise.all([loadAccount({}, "projects")]);
  }
  return Promise.all([loadAccount({}, "system"), loadAccount({}, "projects")]);
}

function updateMobileNavigationAuth() {
  const chatButton = el("mobile-nav-chat");
  if (!chatButton) return;
  const signedIn = Object.values(state.accounts || {}).some(account => Boolean(account?.account));
  chatButton.classList.toggle("hidden", !signedIn);
}

// ---------------------------------------------------------------------------
// First-run graphical wizard
// ---------------------------------------------------------------------------

async function enterSetupWizard() {
  state.setup.active = true;
  state.setup.step = Number(sessionStorage.getItem("clc-setup-step") || 0);
  sessionStorage.removeItem("clc-setup-step");
  selectors.setupOverlay.classList.remove("hidden");
  await refreshSetupState();
}

async function refreshSetupState() {
  try {
    const data = await api("/api/setup/state");
    state.setup.data = data;
    state.projects = data.projects || [];
    state.setup.startAtLogin = data.app?.start_at_login ?? state.setup.startAtLogin;
    if (data.configuration?.remote_enabled) state.setup.remoteChoice = "remote";
    if (!state.setup.cloudChoiceTouched && data.cloud_sync?.provider) state.setup.cloudChoice = data.cloud_sync.provider;
    const legacy = data.bridge || {};
    state.bridges = {
      system: {...(data.bridges?.system || legacy)},
      projects: {...(data.bridges?.projects || {})},
    };
    syncActiveBridgeUI();
    await Promise.all([
      state.bridges.system.initialized ? loadAccount({noRender:true}, "system") : Promise.resolve(),
      state.bridges.projects.initialized ? loadAccount({noRender:true}, "projects") : Promise.resolve(),
    ]);
    renderSetupWizard();
  } catch (error) {
    selectors.setupBody.innerHTML = `<div class="setup-page"><h1>Falha na verificação</h1><p>${escapeHTML(error.message)}</p></div>`;
    toast(error.message, "error");
  }
}

function setupStatusLine(kind, title, detail) {
  const symbol = kind === "ok" ? "✓" : kind === "error" ? "!" : "•";
  return `<div class="status-line"><span class="status-symbol ${kind}">${symbol}</span><div class="status-copy"><strong>${escapeHTML(title)}</strong><small>${escapeHTML(detail)}</small></div></div>`;
}

function projectListHTML(projects, setupMode = false) {
  if (!projects.length) return '<div class="inline-notice">Nenhuma pasta cadastrada.</div>';
  return `<div class="project-setup-list">${projects.map(project => `
    <div class="project-setup-item">
      <span class="status-symbol ok">✓</span>
      <div class="project-copy"><strong>${escapeHTML(project.name)}</strong><small>${escapeHTML(project.path)}</small></div>
      <button class="icon-button" data-project-rename="${escapeHTML(project.id)}" title="Renomear">✎</button>
      ${setupMode || projects.length > 1 ? `<button class="icon-button" data-project-delete="${escapeHTML(project.id)}" title="Remover">×</button>` : ""}
    </div>`).join("")}</div>`;
}

function renderSetupWizard() {
  if (!state.setup.active || !state.setup.data) return;
  const data = state.setup.data;
  const step = state.setup.step;
  document.querySelectorAll("#setup-progress li").forEach(item => {
    const value = Number(item.dataset.step);
    item.classList.toggle("active", value === step);
    item.classList.toggle("complete", value < step);
  });
  selectors.setupBack.disabled = step === 0;
  selectors.setupNext.textContent = step === 6 ? "Concluir configuração" : "Continuar";
  selectors.setupNext.disabled = !canAdvanceSetup();
  el("setup-version").textContent = `v${data.app?.version || state.session?.app_version || "0.9.0"}`;

  if (step === 0) renderSetupWelcome(data);
  else if (step === 1) renderSetupCodex(data);
  else if (step === 2) renderSetupExperience(data);
  else if (step === 3) renderSetupProjects(data);
  else if (step === 4) renderSetupCloud(data);
  else if (step === 5) renderSetupRemote(data);
  else renderSetupFinish(data);
}

function renderSetupWelcome(data) {
  const system = data.system || {};
  selectors.setupBody.innerHTML = `<div class="setup-page">
    <h1>Bem-vindo</h1>
    <p>O assistente prepara o Codex, suas pastas, o acesso pelo Android e a inicialização automática. Nenhuma etapa exige abrir um terminal ou digitar comandos.</p>
    <div class="setup-grid">
      <article class="setup-card">${setupStatusLine("ok", "Aplicativo instalado", `${data.app?.name || "Codex Linux Control"} ${data.app?.version || ""}`)}</article>
      <article class="setup-card">${setupStatusLine("ok", "Sistema compatível", system.name || "Ubuntu/Debian Linux")}</article>
      <article class="setup-card">${setupStatusLine(system.zenity ? "ok" : "error", "Janelas gráficas", system.zenity ? "Seletor de pastas disponível" : "Zenity não foi localizado; reinstale o pacote .deb")}</article>
      <article class="setup-card">${setupStatusLine(system.pkexec ? "ok" : "error", "Autorização administrativa", system.pkexec ? "O broker local executa ações autorizadas sem expor sua senha sudo ao Codex" : "O pacote administrativo será reparado automaticamente")}</article>
      <article class="setup-card full">
        <h3>Como será o uso diário</h3>
        <p>Depois desta configuração, basta abrir o ícone no menu do Linux ou no Android, escolher um projeto e conversar. Comandos ficam ocultos na operação normal e ações sensíveis aparecem como botões de aprovação.</p>
      </article>
    </div>
  </div>`;
}

function renderSetupCodex(data) {
  const codex = data.codex || {};
  const bridges = data.bridges || {system:data.bridge || {}, projects:{}};
  const systemAccount = state.accounts.system;
  const projectAccount = state.accounts.projects;
  const systemConnected = Boolean(systemAccount?.account);
  const projectsConnected = Boolean(projectAccount?.account);
  const accountLine = (workspace, label, bridge, account, connected) => `
      <article class="setup-card">
        ${setupStatusLine(bridge.initialized ? "ok" : "warn", `${label}: ${bridge.initialized ? "serviço pronto" : "serviço pendente"}`, bridge.initialized ? "app-server local e independente" : (bridge.last_error || "Aguardando instalação"))}
        ${setupStatusLine(connected ? "ok" : "warn", connected ? "Conta conectada" : "Conectar conta ChatGPT", connected ? (account?.account?.email || "Autenticação concluída") : "A identidade pode ser a mesma ou diferente do outro workspace")}
        <div class="card-actions"><button class="secondary-button" data-setup-action="login-codex-${workspace}" ${!bridge.initialized ? "disabled" : ""}>${connected ? "Verificar conta" : "Entrar com ChatGPT"}</button></div>
      </article>`;
  selectors.setupBody.innerHTML = `<div class="setup-page">
    <h1>Preparar os dois Codex</h1>
    <p>O Control Plane e os projetos usam processos, usuários Linux, memória e histórico independentes. Ambos são acessados pela mesma interface remota.</p>
    <div class="setup-grid">
      <article class="setup-card full">
        ${setupStatusLine(codex.installed ? "ok" : "warn", codex.installed ? "Codex instalado" : "Codex ainda não instalado", codex.installed ? `${codex.version || "Instalado"}\n${codex.path || ""}` : "Selecione o botão abaixo para instalar")}
        <div class="card-actions"><button class="primary-button" data-setup-action="install-codex">${codex.installed ? "Atualizar Codex" : "Instalar Codex"}</button></div>
      </article>
      ${accountLine("system", "Sistema / Control Plane", bridges.system || {}, systemAccount, systemConnected)}
      ${accountLine("projects", "Projetos / Workers", bridges.projects || {}, projectAccount, projectsConnected)}
      <article class="setup-card full"><h3>Fronteira de autonomia</h3><p>Projetos administram autonomamente a VM Ubuntu Server, sites, aplicativos, bancos e serviços de <strong>sasocq.com</strong>. Somente o Control Plane administra o host físico, Steam, usuários, discos e KVM.</p></article>
    </div>
    ${codex.installed ? '<div class="inline-notice success">Os dois workspaces estão preparados. Os logins também poderão ser concluídos depois em Configurações.</div>' : '<div class="inline-notice">A instalação do Codex é necessária para continuar.</div>'}
  </div>`;
}

function renderSetupExperience(data) {
  const full = data.full_experience || {};
  const desktop = full.desktop || {};
  const browser = full.playwright || {};
  const installed = Boolean(full.installed);
  const needsRelogin = installed && desktop.session_type === "wayland" && desktop.uinput_present && !desktop.uinput_access;
  selectors.setupBody.innerHTML = `<div class="setup-page">
    <h1>Instalar a experiência completa</h1>
    <p>Esta etapa prepara navegador controlado, skills, servidores MCP, ferramentas de desenvolvimento e controle supervisionado da área de trabalho. Tudo é instalado pela interface gráfica.</p>
    <div class="setup-grid">
      <article class="setup-card">${setupStatusLine(installed ? "ok" : "warn", installed ? "Pacotes adicionais instalados" : "Instalar pacotes adicionais", installed ? "Git, ferramentas de código, captura de tela, acessibilidade e automação disponíveis" : "O Linux solicitará sua senha em uma janela do sistema")}
        <div class="card-actions"><button class="primary-button" data-setup-action="install-full-experience">${installed ? "Reparar ou atualizar" : "Instalar experiência completa"}</button></div>
      </article>
      <article class="setup-card">${setupStatusLine(browser.installed && browser.browser_downloaded ? "ok" : "warn", "Navegador Playwright", browser.installed && browser.browser_downloaded ? "Chromium exclusivo com perfil persistente pronto" : "Será baixado um Chromium gerenciado pelo aplicativo")}</article>
      <article class="setup-card">${setupStatusLine(desktop.at_spi && desktop.screenshot !== "unavailable" ? "ok" : "warn", "Controle supervisionado do Linux", `Sessão ${desktop.session_type || "desconhecida"} • entrada ${desktop.input_backend || "indisponível"} • captura ${desktop.screenshot || "indisponível"}`)}</article>
      <article class="setup-card">${setupStatusLine(full.node?.installed ? "ok" : "warn", "Ambiente de extensões", full.node?.installed ? `${full.node.version || "Node LTS"} instalado isoladamente` : "Node LTS, Playwright MCP e skills serão instalados sem alterar seus projetos")}</article>
      <article class="setup-card full">
        <h3>O que ficará disponível</h3>
        <p>Skills e apps por conversa, servidores MCP, navegação visual, perfil persistente do navegador, capturas da tela, janelas, mouse, teclado e acesso pelo Android. Ações externas e de entrada permanecem sujeitas a aprovação.</p>
      </article>
    </div>
    ${needsRelogin ? '<div class="inline-notice">O acesso ao dispositivo virtual de entrada foi concedido. Encerre e entre novamente na sessão do Linux depois de concluir o assistente para ativar mouse e teclado no Wayland.</div>' : ""}
    ${installed ? '<div class="inline-notice success">A experiência completa está instalada. Você pode continuar.</div>' : '<div class="inline-notice">Esta etapa é necessária para entregar a experiência mais próxima do aplicativo desktop.</div>'}
  </div>`;
}

function renderSetupProjects(data) {
  const projects = data.projects || [];
  selectors.setupBody.innerHTML = `<div class="setup-page">
    <h1>Escolher as pastas de trabalho</h1>
    <p>Uma janela nativa do Linux será aberta. O Codex somente poderá escrever nas pastas escolhidas e em suas subpastas.</p>
    <div class="setup-card full">
      <div class="status-line"><span class="status-symbol ${projects.length ? "ok" : "warn"}">${projects.length ? "✓" : "•"}</span><div class="status-copy"><strong>${projects.length ? `${projects.length} projeto(s) cadastrado(s)` : "Escolha ao menos uma pasta"}</strong><small>Você poderá adicionar, renomear ou remover projetos depois, sempre pela interface gráfica.</small></div></div>
      ${projectListHTML(projects, true)}
      <div class="card-actions"><button class="primary-button" data-setup-action="add-project">Escolher pasta no Linux</button></div>
    </div>
  </div>`;
}


function cloudQuestionHTML(session) {
  const option = session?.question;
  if (!option) return "";
  const examples = Array.isArray(option.Examples) ? option.Examples : [];
  const defaultValue = option.Default === undefined || option.Default === null ? "" : String(option.Default);
  const input = examples.length ? `<select id="cloud-question-answer">${examples.map(example => `<option value="${escapeHTML(example.Value)}" ${String(example.Value) === defaultValue ? "selected" : ""}>${escapeHTML(example.Help || example.Value)}</option>`).join("")}${!option.Exclusive && defaultValue && !examples.some(item => String(item.Value) === defaultValue) ? `<option value="${escapeHTML(defaultValue)}" selected>${escapeHTML(defaultValue)}</option>` : ""}</select>` : `<input id="cloud-question-answer" type="${option.IsPassword ? "password" : "text"}" value="${escapeHTML(defaultValue)}" ${option.Required ? "required" : ""} autocomplete="off">`;
  return `<article class="setup-card full">
    <h3>Concluir a configuração da conta</h3>
    ${session.error ? `<div class="inline-notice error">${escapeHTML(session.error)}</div>` : ""}
    <div class="cloud-question-help">${escapeHTML(option.Help || option.Name || "Escolha a opção adequada.")}</div>
    <div class="cloud-form"><label class="field-label">${escapeHTML(option.Name || "Resposta")}${input}</label></div>
    <div class="card-actions"><button class="primary-button" data-setup-action="cloud-answer">Continuar autorização</button></div>
  </article>`;
}

function backupCloudSetupHTML(backup) {
  const session = backup?.session || null;
  const question = session?.question || null;
  const examples = Array.isArray(question?.Examples) ? question.Examples : [];
  const answer = question ? (examples.length
    ? `<select id="setup-backup-cloud-answer">${examples.map(item => `<option value="${escapeHTML(item.Value)}">${escapeHTML(item.Help || item.Value)}</option>`).join("")}</select>`
    : `<input id="setup-backup-cloud-answer" type="${question.IsPassword ? "password" : "text"}" value="${escapeHTML(question.Default ?? "")}" autocomplete="off">`) : "";
  return `<div class="setup-grid" style="margin-top:22px">
    <article class="setup-card full">
      <h2>OneDrive exclusivo para os backups do servidor</h2>
      <p>Este login é independente da identidade Microsoft/Tailscale usada pelo Authenticator e também independente da conta que sincroniza os projetos. Você pode escolher a mesma conta deliberadamente, mas o instalador nunca fará essa associação sozinho.</p>
      ${backup?.configured ? `<div class="inline-notice success">Conta de backup conectada: ${escapeHTML(backup.provider_label || "Microsoft OneDrive")} • remote ${escapeHTML(backup.remote_name || "configurado")}</div>` : '<div class="inline-notice">Conecte a conta que receberá os snapshots criptografados do servidor e da infraestrutura.</div>'}
      <div class="cloud-form"><label class="field-label">Pasta dos backups no OneDrive<input id="setup-backup-cloud-remote-path" type="text" value="${escapeHTML(backup?.remote_path || DEFAULT_BACKUP_REMOTE_PATH)}" autocomplete="off" placeholder="SASOCQ/Backups/Servidor"><small>Você pode escolher esta pasta antes de conectar. A seleção será mantida durante a autorização.</small></label></div>
      ${session?.restart_required ? `<div class="inline-notice error">${escapeHTML(session.error || "O login foi interrompido; conecte novamente.")}</div>` : ""}
      ${question ? `<div class="cloud-form"><label class="field-label">${escapeHTML(question.Name || "Resposta")}${answer}</label></div>` : ""}
      <div class="card-actions">
        ${!backup?.installed ? '<button class="secondary-button" data-setup-action="backup-cloud-install">Instalar suporte</button>' : ""}
        ${!backup?.configured && !question ? '<button class="primary-button" data-setup-action="backup-cloud-connect">Conectar conta OneDrive de backup</button>' : ""}
        ${question ? '<button class="primary-button" data-setup-action="backup-cloud-answer">Continuar login do backup</button>' : ""}
        ${backup?.configured ? '<button class="secondary-button" data-setup-action="backup-cloud-activate">Salvar pasta de backup</button>' : ""}
      </div>
    </article>
  </div>`;
}

function renderSetupCloud(data) {
  const cloud = data.cloud_sync || {};
  const backupCloud = data.backup_cloud || {};
  const synchronizerReady = Boolean(cloud.installed && cloud.compatible);
  const choice = state.setup.cloudChoice;
  const selectedProvider = choice === "google_drive" || choice === "onedrive";
  const configuredForChoice = selectedProvider && cloud.configured && cloud.provider === choice;
  const initializedForChoice = configuredForChoice && cloud.initialized;
  const session = cloud.session && cloud.session.provider === choice ? cloud.session : null;
  const localPath = cloud.local_path || "~/CodexProjects";
  const remotePath = cloud.remote_path || "Codex Linux Control/Projetos";
  const outsideProjects = (data.projects || []).filter(project => {
    const path = String(project.path || "");
    return path !== localPath && !path.startsWith(`${localPath}/`);
  });
  const last = cloud.status || {};
  const lastText = last.finished_at ? `${last.ok ? "Concluída" : "Falhou"} em ${formatTime(last.finished_at)}` : "Ainda não executada";
  selectors.setupBody.innerHTML = `<div class="setup-page">
    <h1>Sincronizar os projetos na nuvem</h1>
    <p>Escolha Google Drive ou OneDrive. O aplicativo instala e configura o sincronizador, centraliza os projetos e informa claramente a pasta local onde todos eles ficam.</p>
    <div class="choice-list">
      <label class="choice-card"><input type="radio" name="cloud-choice" value="onedrive" ${choice === "onedrive" ? "checked" : ""}><span class="cloud-provider-logo">1D</span><span><strong>Microsoft OneDrive</strong><small>Autorização direta pela conta Microsoft; recomendado para configuração mais simples.</small></span></label>
      <label class="choice-card"><input type="radio" name="cloud-choice" value="google_drive" ${choice === "google_drive" ? "checked" : ""}><span class="cloud-provider-logo">GD</span><span><strong>Google Drive</strong><small>Exige um Client ID próprio do Google, preenchido inteiramente nesta tela.</small></span></label>
    </div>
    <div class="setup-grid" style="margin-top:16px">
      <article class="setup-card">${setupStatusLine(synchronizerReady ? "ok" : "warn", synchronizerReady ? "Sincronizador atualizado" : cloud.installed ? "Atualizar o sincronizador" : "Instalar o sincronizador", synchronizerReady ? (cloud.version || "rclone disponível") : cloud.installed ? `${cloud.version || "Versão antiga"} — é necessária a versão ${cloud.minimum_version || "1.71.0"} ou superior` : "O Linux solicitará sua senha em uma janela do sistema")}
        <div class="card-actions"><button class="primary-button" data-setup-action="cloud-install">${synchronizerReady ? "Verificar ou reparar" : cloud.installed ? "Atualizar sincronização" : "Instalar sincronização"}</button></div>
      </article>
      <article class="setup-card">${setupStatusLine(configuredForChoice ? "ok" : "warn", configuredForChoice ? `${cloud.provider_label || "Conta"} conectado` : "Conectar a conta", configuredForChoice ? `${cloud.remote_name}: autorizado` : "A autorização será aberta no navegador; sua senha não passa pelo aplicativo")}</article>
      ${synchronizerReady && !configuredForChoice ? `<article class="setup-card full">
        <h3>${choice === "google_drive" ? "Credenciais do aplicativo Google" : "Autorizar o OneDrive"}</h3>
        <p>${choice === "google_drive" ? "O Client ID próprio evita depender do cliente compartilhado do rclone. O segredo é ocultado nos registros e armazenado no arquivo de configuração criptografado." : "Na maioria das contas não é necessário informar Client ID ou segredo. Basta abrir a autorização Microsoft."}</p>
        ${choice === "google_drive" ? `<div class="cloud-form two-column"><label class="field-label">Google Client ID<input id="cloud-client-id" type="text" autocomplete="off" placeholder="...apps.googleusercontent.com"></label><label class="field-label">Google Client Secret<input id="cloud-client-secret" type="password" autocomplete="new-password" placeholder="Segredo do cliente"></label></div><div class="card-actions"><button class="secondary-button" data-setup-action="cloud-google-help">Abrir guia para criar o Client ID</button><button class="primary-button" data-setup-action="cloud-connect">Conectar Google Drive</button></div>` : `<div class="card-actions"><button class="primary-button" data-setup-action="cloud-connect">Conectar Microsoft OneDrive</button></div>`}
      </article>` : ""}
      ${session?.restart_required ? `<article class="setup-card full"><div class="inline-notice error">${escapeHTML(session.error || "A autorização anterior foi interrompida.")}</div><div class="card-actions"><button class="primary-button" data-setup-action="cloud-connect">Conectar novamente</button></div></article>` : session && !session.complete ? cloudQuestionHTML(session) : ""}
      ${configuredForChoice ? `<article class="setup-card full">
        <h3>Pasta central dos projetos</h3>
        <p>Todos os projetos sincronizados serão reunidos nesta pasta local. A pasta na nuvem é criada automaticamente.</p>
        <div class="cloud-form two-column"><label class="field-label">Pasta local<input id="cloud-local-path" type="text" value="${escapeHTML(localPath)}"></label><label class="field-label">Pasta em ${escapeHTML(cloud.provider_label || "nuvem")}<input id="cloud-remote-path" type="text" value="${escapeHTML(remotePath)}"></label></div>
        <div class="card-actions"><button class="secondary-button" data-setup-action="cloud-pick-folder">Escolher pasta no Linux</button><button class="primary-button" data-setup-action="cloud-browse-folder">Escolher pasta no OneDrive</button><button class="secondary-button" data-setup-action="cloud-save-folders">Salvar pastas</button>${outsideProjects.length ? '<button class="primary-button" data-setup-action="cloud-consolidate">Copiar projetos cadastrados para a pasta central</button>' : ""}</div>
        ${outsideProjects.length ? `<div class="inline-notice">${outsideProjects.length} projeto(s) ainda estão fora da pasta central. A cópia preservará os arquivos originais e atualizará o cadastro do Codex para a nova localização.</div>` : '<div class="inline-notice success">Os projetos cadastrados já estão dentro da pasta central.</div>'}
      </article>
      <article class="setup-card full">
        <h3>${initializedForChoice ? "Sincronização ativa" : "Primeira sincronização"}</h3>
        <p>A sincronização bidirecional preserva conflitos como cópias separadas, mantém versões substituídas em pastas de segurança e interrompe exclusões em massa.</p>
        <div class="cloud-form two-column"><label class="field-label">Conteúdo sincronizado<select id="cloud-filter-profile"><option value="source" ${cloud.filter_profile !== "complete" ? "selected" : ""}>Código seguro — exclui caches, segredos e .git</option><option value="complete" ${cloud.filter_profile === "complete" ? "selected" : ""}>Projeto completo — inclui tudo</option></select></label>${!initializedForChoice ? `<label class="field-label">Prioridade na primeira sincronização<select id="cloud-initial-strategy"><option value="path1" ${state.setup.cloudStrategy === "path1" ? "selected" : ""}>Este Linux é a fonte principal</option><option value="path2" ${state.setup.cloudStrategy === "path2" ? "selected" : ""}>A nuvem é a fonte principal</option><option value="newer" ${state.setup.cloudStrategy === "newer" ? "selected" : ""}>Manter a versão mais recente</option></select></label>` : `<label class="field-label">Sincronização automática<select id="cloud-interval"><option value="5" ${Number(cloud.interval_minutes) === 5 ? "selected" : ""}>A cada 5 minutos</option><option value="15" ${Number(cloud.interval_minutes) === 15 ? "selected" : ""}>A cada 15 minutos</option><option value="30" ${Number(cloud.interval_minutes) === 30 ? "selected" : ""}>A cada 30 minutos</option><option value="60" ${Number(cloud.interval_minutes) === 60 ? "selected" : ""}>A cada hora</option></select></label>`}</div>
        <div class="card-actions"><button class="primary-button" data-setup-action="${initializedForChoice ? "cloud-sync-now" : "cloud-initial-sync"}">${initializedForChoice ? "Sincronizar agora" : "Iniciar sincronização"}</button></div>
        <div class="inline-notice ${last.ok ? "success" : last.error ? "error" : ""}">Última execução: ${escapeHTML(lastText)}${last.error ? ` — ${escapeHTML(String(last.error).slice(0, 300))}` : ""}</div>
      </article>
      ${initializedForChoice ? `<article class="setup-card full"><h3>Onde estão todos os projetos sincronizados</h3><span class="cloud-path">${escapeHTML(localPath)}</span><div class="card-actions"><button class="primary-button" data-setup-action="cloud-open-folder">Abrir a pasta</button><button class="secondary-button" data-setup-action="cloud-copy-path">Copiar caminho</button></div></article>` : ""}
      ` : ""}
    </div>
    ${backupCloudSetupHTML(backupCloud)}
  </div>`;
}

function renderSetupRemote(data) {
  const tailscale = data.tailscale || {};
  const config = data.configuration || {};
  const security = data.security || {};
  const entra = security.entra || {};
  const devices = security.paired_devices || [];
  const remote = state.setup.remoteChoice === "remote";
  const entraReady = Boolean(entra.configured && entra.verified);
  const secureReady = Boolean(entraReady && (!remote || (config.remote_enabled && devices.length >= 2)));
  selectors.setupBody.innerHTML = `<div class="setup-page">
    <h1>Identidade administrativa e acesso remoto</h1>
    <p>A conta de administração Microsoft é independente das contas OneDrive. O celular e o tablet continuam pareados separadamente, e ações críticas exigem nova confirmação no Microsoft Authenticator.</p>
    <div class="setup-grid">
      <article class="setup-card full">
        <h3>Microsoft Entra / Authenticator</h3>
        ${setupStatusLine(entraReady ? "ok" : entra.configured ? "warn" : "error", entraReady ? "Identidade administrativa confirmada" : entra.configured ? "Aplicativo Microsoft configurado; falta confirmar a identidade" : "Cadastre o aplicativo Microsoft Entra", entraReady ? `${entra.email || "Identidade Microsoft"} • MFA/passkey comprovado` : "Use um aplicativo público OIDC com PKCE e a URL de redirecionamento mostrada abaixo")}
        <div class="cloud-form two-column">
          <label class="field-label">Tenant Microsoft Entra<input id="entra-tenant" type="text" autocomplete="off" value="${escapeHTML(entra.tenant || "")}" placeholder="Directory/Tenant ID específico"></label>
          <label class="field-label">Client ID do aplicativo<input id="entra-client-id" type="text" autocomplete="off" value="${escapeHTML(entra.client_id || "")}" placeholder="00000000-0000-0000-0000-000000000000"></label>
          <label class="field-label full">Authentication Context/ACR obrigatório para passkey<input id="entra-required-acr" type="text" autocomplete="off" value="${escapeHTML(entra.required_acr || "")}" placeholder="Ex.: c1 — política resistente a phishing"></label>
        </div>
        <div class="inline-notice"><strong>Redirect URI:</strong> ${escapeHTML(entra.redirect_uri || "Será calculada pelo aplicativo")}</div>
        <div class="card-actions">
          <button class="secondary-button" data-setup-action="entra-help">Abrir Microsoft Entra</button>
          <button class="primary-button" data-setup-action="entra-configure">Salvar configuração</button>
          <button class="primary-button" data-setup-action="entra-login" ${!entra.configured ? "disabled" : ""}>Confirmar no Authenticator</button>
        </div>
      </article>
    </div>
    <div class="choice-list" style="margin-top:16px">
      <label class="choice-card"><input type="radio" name="remote-choice" value="local" ${!remote ? "checked" : ""}><span><strong>Somente neste computador</strong><small>O painel não será disponibilizado fora do mini PC.</small></span></label>
      <label class="choice-card"><input type="radio" name="remote-choice" value="remote" ${remote ? "checked" : ""}><span><strong>Celular, tablet e outros PCs</strong><small>Tailscale privado + Authenticator + chave criptográfica individual.</small></span></label>
    </div>
    ${remote ? `<div class="setup-grid" style="margin-top:16px">
      <article class="setup-card">${setupStatusLine(tailscale.installed ? "ok" : "warn", tailscale.installed ? "Rede privada instalada" : "Rede privada será instalada", tailscale.installed ? tailscale.version || "Tailscale instalado" : "O assistente instalará o Tailscale")}</article>
      <article class="setup-card">${setupStatusLine(tailscale.connected ? "ok" : "warn", tailscale.connected ? "Identidade da tailnet confirmada" : "Autorize o Tailscale", tailscale.connected ? (tailscale.login || tailscale.dns_name || "Conectado") : "Esta identidade pode ser diferente da conta usada no OneDrive")}</article>
      <article class="setup-card">${setupStatusLine(config.remote_enabled ? "ok" : "warn", config.remote_enabled ? "HTTPS privado ativado" : "HTTPS será ativado automaticamente", config.remote_enabled ? `${config.external_url || tailscale.external_url}\nIdentidade: ${config.allowed_tailscale_login || tailscale.login}` : "Nenhuma porta administrativa pública será aberta")}</article>
      <article class="setup-card full">
        <h3>${config.remote_enabled ? "Conexão privada configurada" : "Preparar tudo em uma única etapa"}</h3>
        <p>O aplicativo instala a rede privada, ativa o HTTPS interno e mantém o backend em localhost.</p>
        <div class="card-actions"><button class="primary-button" data-setup-action="prepare-remote">${config.remote_enabled ? "Verificar e reparar" : "Preparar acesso remoto"}</button>${config.remote_enabled ? '<button class="danger-button" data-setup-action="disable-remote">Desativar</button>' : ""}</div>
      </article>
      <article class="setup-card">${setupStatusLine(devices.length >= 2 ? "ok" : "warn", devices.length >= 2 ? `${devices.length} dispositivos pareados` : `Pareie celular e tablet (${devices.length}/2)`, devices.length >= 2 ? "Cada aparelho tem chave própria e pode ser revogado isoladamente" : "Gere um QR separado para cada aparelho")}
        <div class="card-actions"><button class="primary-button" data-setup-action="pair-device" ${!config.remote_enabled ? "disabled" : ""}>Parear dispositivo</button></div>
      </article>
      <article class="setup-card full"><h3>${secureReady ? "Acesso seguro pronto" : "Configuração ainda incompleta"}</h3><p>O acesso exige a tailnet autorizada, a chave ECDSA daquele navegador e a identidade administrativa confirmada pelo Microsoft Authenticator.</p></article>
    </div>` : `<div class="inline-notice ${entraReady ? "success" : "error"}">${entraReady ? "Identidade administrativa pronta; o painel permanecerá apenas em localhost." : "Confirme a identidade Microsoft antes de concluir."}</div>`}
  </div>`;
}

function renderSetupFinish(data) {
  const config = data.configuration || {};
  const codex = data.codex || {};
  const projects = data.projects || [];
  const remote = state.setup.remoteChoice === "remote";
  const full = data.full_experience || {};
  const pairedDevices = data.security?.paired_devices || [];
  const cloud = data.cloud_sync || {};
  const backupCloud = data.backup_cloud || {};
  const cloudSelected = true;
  selectors.setupBody.innerHTML = `<div class="setup-page">
    <h1>Pronto para concluir</h1>
    <p>Revise o resumo. Depois deste botão, a interface principal será aberta e toda a manutenção continuará disponível em Configurações.</p>
    <div class="summary-list">
      <div class="summary-row"><strong>Codex</strong><span>${escapeHTML(codex.version || "Instalado")}</span></div>
      <div class="summary-row"><strong>Experiência completa</strong><span>${full.installed ? "Navegador, MCPs e desktop preparados" : "Pendente"}</span></div>
      <div class="summary-row"><strong>Projetos</strong><span>${projects.length} pasta(s) autorizada(s)</span></div>
      <div class="summary-row"><strong>Nuvem dos projetos</strong><span>${`${escapeHTML(cloud.provider_label || "Nuvem")} ↔ ${escapeHTML(cloud.local_path || "~/CodexProjects")}`}</span></div>
      <div class="summary-row"><strong>OneDrive dos backups</strong><span>${backupCloud.configured ? "Conta independente conectada" : "Pendente"}</span></div>
      <div class="summary-row"><strong>Administração Microsoft</strong><span>${data.security?.entra?.verified ? `${escapeHTML(data.security.entra.email || "Identidade confirmada")} • Authenticator` : "Pendente"}</span></div>
      <div class="summary-row"><strong>Acesso</strong><span>${remote ? escapeHTML(config.external_url || "Tailscale privado") : "Somente local"}</span></div>
      <div class="summary-row"><strong>Identidade externa</strong><span>${remote ? escapeHTML(config.allowed_tailscale_login || "Detectada pelo Tailscale") : "Não aplicável"}</span></div>
      <div class="summary-row"><strong>Dispositivos autorizados</strong><span>${remote ? `${pairedDevices.length} chave(s) pareada(s)` : "Não aplicável"}</span></div>
      <div class="summary-row"><strong>Tela remota</strong><span>Adaptação automática para celular, tablet e PC</span></div>
      <div class="summary-row"><strong>Sistema SASOCQ</strong><span>${data.control?.available ? `Broker ativo • provisionamento ${escapeHTML(data.control?.provision?.status || "em andamento")}` : "Controle local pendente"}</span></div>
    </div>
    <article class="setup-card full switch-row">
      <div><h3>${remote ? "Iniciar automaticamente após ligar o Linux" : "Iniciar automaticamente ao entrar no Linux"}</h3><p>${remote ? "Obrigatório para manter o painel disponível mesmo sem login local; o assistente configura isso automaticamente." : "Mantém o painel pronto durante sua sessão, sem abrir janelas."}</p></div>
      <label class="switch"><input id="setup-autostart" type="checkbox" ${(state.setup.startAtLogin || remote) ? "checked" : ""} ${remote ? "disabled" : ""}><span></span></label>
    </article>
  </div>`;
}

function canAdvanceSetup() {
  const data = state.setup.data;
  if (!data) return false;
  if (state.setup.step === 1) return Boolean(data.codex?.installed && state.bridges.system.initialized && state.bridges.projects.initialized && state.accounts.system?.account && state.accounts.projects?.account);
  if (state.setup.step === 2) return Boolean(data.full_experience?.installed);
  if (state.setup.step === 3) return Boolean((data.projects || []).some(item => item.kind !== "system"));
  if (state.setup.step === 4) {
    const cloud = data.cloud_sync || {};
    const backup = data.backup_cloud || {};
    return Boolean(cloud.configured && cloud.initialized && cloud.provider === state.setup.cloudChoice && backup.configured);
  }
  if (state.setup.step === 5) {
    const entraReady = Boolean(data.security?.entra?.configured && data.security?.entra?.verified);
    if (state.setup.remoteChoice === "remote") {
      return Boolean(entraReady && data.configuration?.remote_enabled && (data.security?.paired_devices || []).length >= 2);
    }
    return entraReady;
  }
  return true;
}

async function advanceSetup() {
  if (!canAdvanceSetup()) return;
  if (state.setup.step < 6) {
    state.setup.step += 1;
    renderSetupWizard();
    return;
  }
  const auto = el("setup-autostart");
  if (auto) state.setup.startAtLogin = auto.checked;
  selectors.setupNext.disabled = true;
  try {
    await api("/api/setup/finish", {
      method:"POST",
      body:JSON.stringify({start_at_login:state.setup.startAtLogin, remote_access:state.setup.remoteChoice === "remote", cloud_sync:true}),
    });
    const session = await api("/api/session");
    state.session = session;
    state.csrf = session.csrf;
    state.identity = session.identity;
    await initializeMainInterface();
    toast("Configuração concluída.", "success");
  } catch (error) {
    toast(error.message, "error");
    selectors.setupNext.disabled = false;
  }
}

function backSetup() {
  if (state.setup.step > 0) {
    state.setup.step -= 1;
    renderSetupWizard();
  }
}

async function handleSetupAction(action) {
  if (action === "install-codex") return startBackgroundTask("/api/setup/codex/install");
  if (action === "install-full-experience") return startBackgroundTask("/api/setup/full-experience/install");
  if (action === "login-codex-system") return startDeviceLogin("system");
  if (action === "login-codex-projects") return api("/api/account/share-projects", {method:"POST"});
  if (action === "add-project") return addProjectGraphically(true);
  if (action === "cloud-install") return startBackgroundTask("/api/cloud/install");
  if (action === "cloud-google-help") { window.open("https://rclone.org/drive/#making-your-own-client-id", "_blank", "noopener"); return; }
  if (action === "cloud-connect") {
    const provider = state.setup.cloudChoice;
    const client_id = el("cloud-client-id")?.value.trim() || "";
    const client_secret = el("cloud-client-secret")?.value || "";
    return startBackgroundTask("/api/cloud/config/start", {provider, client_id, client_secret});
  }
  if (action === "cloud-answer") {
    const session = state.setup.data?.cloud_sync?.session;
    const answer = el("cloud-question-answer")?.value ?? "";
    if (!session?.id) return toast("A sessão de autorização não está disponível.", "error");
    return startBackgroundTask("/api/cloud/config/answer", {session_id:session.id, answer});
  }
  if (action === "backup-cloud-install") return startBackgroundTask("/api/backup-cloud/install");
  if (action === "backup-cloud-browse") return openOneDriveFolderBrowser("backup");
  if (action === "backup-cloud-connect") return startBackgroundTask("/api/backup-cloud/config/start", {provider:"onedrive", client_id:"", client_secret:"", remote_path:backupRemotePath()});
  if (action === "backup-cloud-answer") {
    const session = state.setup.data?.backup_cloud?.session;
    const answer = el("setup-backup-cloud-answer")?.value ?? "";
    if (!session?.id) return toast("A sessão de autorização do backup não está disponível.", "error");
    return startBackgroundTask("/api/backup-cloud/config/answer", {session_id:session.id, answer, remote_path:backupRemotePath()});
  }
  if (action === "backup-cloud-activate") {
    await api("/api/backup-cloud/activate", {method:"POST", body:JSON.stringify({remote_path:backupRemotePath()})});
    await refreshSetupState(); toast("Destino de backup independente ativado.", "success"); return;
  }
  if (action === "cloud-pick-folder") {
    try { await api("/api/cloud/folder/pick", {method:"POST"}); await refreshSetupState(); toast("Pasta central atualizada.", "success"); }
    catch (error) { if (!/cancelada/i.test(error.message)) toast(error.message, "error"); }
    return;
  }
  if (action === "cloud-save-folders") {
    const local_path = el("cloud-local-path")?.value.trim() || state.setup.data?.cloud_sync?.local_path;
    const remote_path = el("cloud-remote-path")?.value.trim() || "Codex Linux Control/Projetos";
    await api("/api/cloud/folder", {method:"POST", body:JSON.stringify({local_path, remote_path})});
    await refreshSetupState(); toast("Pastas de sincronização salvas.", "success"); return;
  }
  if (action === "cloud-browse-folder") return openOneDriveFolderBrowser("projects");
  if (action === "cloud-consolidate") return startBackgroundTask("/api/cloud/projects/consolidate");
  if (action === "cloud-initial-sync") {
    const profile = el("cloud-filter-profile")?.value || "source";
    state.setup.cloudStrategy = el("cloud-initial-strategy")?.value || state.setup.cloudStrategy;
    await api("/api/cloud/filter", {method:"POST", body:JSON.stringify({profile})});
    return startBackgroundTask("/api/cloud/sync/initial", {strategy:state.setup.cloudStrategy});
  }
  if (action === "cloud-sync-now") {
    const profile = el("cloud-filter-profile")?.value || "source";
    const interval = Number(el("cloud-interval")?.value || state.setup.data?.cloud_sync?.interval_minutes || 5);
    await api("/api/cloud/filter", {method:"POST", body:JSON.stringify({profile})});
    await api("/api/cloud/timer", {method:"POST", body:JSON.stringify({enabled:true, interval_minutes:interval})});
    return startBackgroundTask("/api/cloud/sync/now");
  }
  if (action === "cloud-open-folder") { await api("/api/cloud/open-folder", {method:"POST"}); return; }
  if (action === "cloud-copy-path") {
    const path = state.setup.data?.cloud_sync?.local_path || "";
    try { await navigator.clipboard.writeText(path); toast("Caminho copiado.", "success"); }
    catch { window.prompt("Copie o caminho:", path); }
    return;
  }
  if (action === "entra-help") { window.open("https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade", "_blank", "noopener"); return; }
  if (action === "entra-configure") {
    const tenant = el("entra-tenant")?.value.trim() || "";
    const client_id = el("entra-client-id")?.value.trim() || "";
    const required_acr = el("entra-required-acr")?.value.trim() || "";
    if (!tenant || ["common", "consumers", "organizations"].includes(tenant.toLowerCase())) return toast("Informe o Directory/Tenant ID específico do Microsoft Entra.", "error");
    if (!client_id) return toast("Informe o Client ID do aplicativo Microsoft Entra.", "error");
    if (!required_acr) return toast("Informe o Authentication Context/ACR que exige passkey resistente a phishing.", "error");
    await api("/api/auth/entra/config", {method:"POST", body:JSON.stringify({tenant, client_id, allowed_identities:[], require_mfa:true, required_acr, require_phishing_resistant:true})});
    await refreshSetupState(); toast("Microsoft Entra configurado.", "success"); return;
  }
  if (action === "entra-login") {
    sessionStorage.setItem("clc-setup-step", "5");
    return beginMicrosoftAuthentication(state.session, false);
  }
  if (action === "prepare-remote") return startBackgroundTask("/api/setup/remote/prepare");
  if (action === "install-tailscale") return startBackgroundTask("/api/setup/tailscale/install");
  if (action === "connect-tailscale") return startBackgroundTask("/api/setup/tailscale/connect");
  if (action === "enable-remote") return startBackgroundTask("/api/setup/tailscale/serve");
  if (action === "disable-remote") return startBackgroundTask("/api/setup/tailscale/disable");
  if (action === "pair-device") return showPairingDialog();
}

// ---------------------------------------------------------------------------
// Graphical long-running operations
// ---------------------------------------------------------------------------

async function startBackgroundTask(endpoint, body = null) {
  try {
    const options = {method:"POST"};
    if (body !== null) options.body = JSON.stringify(body);
    const data = await api(endpoint, options);
    if (!data.task) throw new Error("O servidor não iniciou a operação");
    showTaskDialog(data.task);
    pollTask(data.task.id);
  } catch (error) {
    toast(error.message, "error");
  }
}

function showTaskDialog(task) {
  state.activeTaskKind = task.kind || "";
  state.activeTaskCreatedAt = Number(task.created_at || Date.now() / 1000);
  selectors.taskTitle.textContent = task.title || "Operação em andamento";
  updateTaskDialog(task);
  if (!selectors.taskDialog.open) selectors.taskDialog.showModal();
}

function inferredBackupPercent(backup) {
  const reported = Number(backup?.progress?.percent);
  if (Number.isFinite(reported) && reported > 0) return reported;
  const message = String(backup?.message || "").toLowerCase();
  if (backup?.status === "complete" || backup?.status === "warning") return 100;
  if (message.includes("consolidando")) return 96;
  if (message.includes("integridade") || message.includes("verificando")) return 90;
  if (message.includes("retenção")) return 78;
  if (message.includes("copiando") || message.includes("base da vm") || message.includes("arquivos alterados")) return 35;
  if (message.includes("snapshot")) return 12;
  return backup?.status === "running" ? 5 : 0;
}

async function recoverBackupTask(taskId) {
  const control = await api("/api/control/status");
  const backup = control.backup || {};
  const service = String(backup.service || "");
  const running = backup.status === "running" || service.includes("ActiveState=activating") || service.includes("SubState=start");
  const failed = backup.status === "failed";
  return {
    id: taskId,
    kind: "backup-run",
    title: "Backup do servidor",
    status: failed ? "failed" : running ? "running" : "succeeded",
    message: backup.message || (running ? "Backup em andamento…" : failed ? "O backup falhou." : "Backup concluído."),
    error: failed ? backup.message || "O backup falhou." : "",
    logs: [backup.message || "Estado recuperado diretamente do serviço de backup."],
    result: {progress: {percent: inferredBackupPercent(backup), phase: backup.progress?.phase || backup.status}, backup},
    created_at: state.activeTaskCreatedAt || Number(backup.updated_at || Date.now() / 1000),
  };
}

function updateTaskDialog(task) {
  const finished = ["succeeded", "failed", "cancelled"].includes(task.status);
  selectors.taskMessage.textContent = task.message || "Em andamento…";
  const showProgress = task.kind === "backup-run";
  selectors.taskProgress.classList.toggle("hidden", !showProgress);
  if (showProgress) {
    const reported = Number(task.result?.progress?.percent);
    const percent = task.status === "succeeded" ? 100 : Math.max(0, Math.min(100, Number.isFinite(reported) ? reported : 0));
    const elapsedSeconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(task.created_at || Date.now() / 1000)));
    const minutes = Math.floor(elapsedSeconds / 60);
    const seconds = elapsedSeconds % 60;
    selectors.taskProgressPhase.textContent = task.message || "Backup em andamento…";
    selectors.taskProgressPercent.textContent = `${percent}%`;
    selectors.taskProgressFill.style.width = `${percent}%`;
    selectors.taskProgress.setAttribute("aria-valuenow", String(percent));
    selectors.taskProgressElapsed.textContent = `Tempo decorrido: ${minutes ? `${minutes}min ` : ""}${seconds}s`;
  }
  selectors.taskLogs.textContent = (task.logs || []).join("\n") || "Aguardando detalhes…";
  selectors.taskLogs.scrollTop = selectors.taskLogs.scrollHeight;
  selectors.taskSpinner.className = `spinner ${task.status === "succeeded" ? "complete" : task.status === "failed" ? "failed" : ""}`.trim();
  selectors.taskActionLink.classList.toggle("hidden", !task.action_url);
  if (task.action_url) {
    selectors.taskActionLink.href = task.action_url;
    selectors.taskActionLink.textContent = isLoopbackAuthorizationUrl(task.action_url)
      ? "Abrir autorização no mini PC"
      : "Abrir página de autorização";
  }
  selectors.taskDone.disabled = !finished;
  selectors.taskClose.disabled = !finished;
  if (task.status === "succeeded") selectors.taskDone.textContent = "Concluir";
  else if (task.status === "failed") selectors.taskDone.textContent = "Fechar";
}

function isLoopbackAuthorizationUrl(value) {
  try {
    const url = new URL(value, location.href);
    return url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  } catch {
    return false;
  }
}

function pollTask(taskId) {
  if (state.taskPoll) clearTimeout(state.taskPoll);
  const run = async () => {
    try {
      const task = await api(`/api/setup/tasks/${encodeURIComponent(taskId)}`);
      updateTaskDialog(task);
      if (["succeeded", "failed", "cancelled"].includes(task.status)) {
        state.taskPoll = null;
        if (task.status === "succeeded") {
          toast(task.message || "Operação concluída.", "success");
          if (state.setup.active) {
            await refreshSetupState();
            if (task.kind === "remote-access-prepare" && !(state.setup.data?.security?.paired_devices || []).length) {
              if (selectors.taskDialog.open) selectors.taskDialog.close();
              await showPairingDialog();
            }
          } else {
            await loadStatus().catch(() => null);
            await loadProjects().catch(() => null);
            await Promise.all([
              state.bridges.system.initialized ? loadAccount({}, "system").catch(() => null) : Promise.resolve(),
              state.bridges.projects.initialized ? loadAccount({}, "projects").catch(() => null) : Promise.resolve(),
            ]);
          }
        } else toast(task.error || task.message || "A operação falhou.", "error");
        return;
      }
      state.taskPoll = setTimeout(run, 900);
    } catch (error) {
      if (state.activeTaskKind === "backup-run") {
        try {
          const recovered = await recoverBackupTask(taskId);
          updateTaskDialog(recovered);
          if (["succeeded", "failed"].includes(recovered.status)) {
            state.taskPoll = null;
            if (recovered.status === "succeeded") toast(recovered.message, "success");
            else toast(recovered.error || recovered.message, "error");
            await loadStatus().catch(() => null);
            return;
          }
        } catch (recoveryError) {
          selectors.taskMessage.textContent = recoveryError.message;
        }
      } else selectors.taskMessage.textContent = error.message;
      state.taskPoll = setTimeout(run, 1800);
    }
  };
  run();
}

// ---------------------------------------------------------------------------
// Projects, threads and conversation UI
// ---------------------------------------------------------------------------

function closeProjectMenu() {
  state.projectMenu?.remove();
  state.projectMenu = null;
}

function projectMenuButton(label, action, danger = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  if (danger) button.className = "danger";
  button.addEventListener("click", async () => {
    closeProjectMenu();
    await action();
  });
  return button;
}

function openProjectMenu(project, anchor) {
  closeProjectMenu();
  const menu = document.createElement("div");
  menu.className = "floating-row-menu project-options-menu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", `Ações de ${project.name}`);
  const configuredPaths = [...new Set([project.clc?.main_path, project.path, ...(project.clc?.paths || [])].filter(Boolean))];
  const location = document.createElement("div");
  location.className = "project-location";
  location.innerHTML = `<small>${project.kind === "system" ? "Pasta do sistema" : "Pasta principal do projeto"}</small><strong>${escapeHTML(configuredPaths[0] || "Não informada")}</strong>${configuredPaths.slice(1).map(path => `<span><b>Pasta adicional</b>${escapeHTML(path)}</span>`).join("")}`;
  menu.append(location);
  menu.append(
    projectMenuButton("＋ Nova conversa", async () => {
      await selectProject(project.id);
      await newThread();
    }),
    projectMenuButton("Abrir projeto", () => selectProject(project.id)),
    projectMenuButton("Copiar caminho da pasta", async () => {
      await navigator.clipboard.writeText(configuredPaths[0] || project.path || "");
      toast("Caminho da pasta copiado.", "success");
    }),
    projectMenuButton(project.clc?.pinned ? "Desafixar projeto" : "Fixar projeto", () => toggleProjectPinned(project)),
  );
  if (project.kind !== "system") {
    menu.append(
      projectMenuButton("Renomear", () => renameProject(project.id)),
      projectMenuButton("Arquivar todas as conversas", () => archiveProjectThreads(project)),
      projectMenuButton("Remover somente da lista", () => deleteProjectGraphically(project.id)),
      projectMenuButton("Excluir pasta e projeto…", () => deleteProjectFiles(project), true),
    );
  }
  document.body.appendChild(menu);
  const anchorRect = anchor.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  const left = Math.max(8, Math.min(anchorRect.right - menuRect.width, window.innerWidth - menuRect.width - 8));
  const below = anchorRect.bottom + 6;
  const top = below + menuRect.height <= window.innerHeight - 8
    ? below
    : Math.max(8, anchorRect.top - menuRect.height - 6);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  state.projectMenu = menu;
}

function markProjectUsed(projectId, timestamp = Date.now()) {
  if (!projectId) return;
  state.projectUsage.set(projectId, timestamp);
  try {
    localStorage.setItem(PROJECT_USAGE_STORAGE_KEY, JSON.stringify(Object.fromEntries(state.projectUsage)));
  } catch {
    // Conversation activity still provides a stable ordering if storage is unavailable.
  }
}

function projectLastUsedTimestamp(project) {
  return Math.max(
    Number(state.projectActivity.get(project.id) || 0),
    Number(state.projectUsage.get(project.id) || 0),
  );
}

function createProjectRow(project) {
  const item = document.createElement("div");
  item.className = `nav-item project-nav-item ${state.activeProject?.id === project.id ? "active" : ""}`;
  item.dataset.projectId = project.id;
  const system = project.kind === "system";
  const initials = system ? "SYS" : project.name.split(/\s+/).slice(0,2).map(part => part[0]).join("").toUpperCase();
  const activity = projectLastUsedTimestamp(project);
  const lastUsed = activity ? `Último uso: ${formatTime(activity)}` : "Sem atividade registrada";
  const preview = system ? "Control Plane permanente • host, VM e Steam" : `${lastUsed} • ${project.path}`;
  const menuButton = document.createElement("button");
  menuButton.type = "button";
  menuButton.className = "nav-icon project-icon-menu";
  menuButton.setAttribute("aria-label", `Opções de ${project.name}`);
  menuButton.setAttribute("aria-haspopup", "menu");
  menuButton.title = `Opções de ${project.name}`;
  menuButton.textContent = initials || "P";
  menuButton.addEventListener("click", event => {
    event.stopPropagation();
    openProjectMenu(project, menuButton);
  });
  const openButton = document.createElement("button");
  openButton.type = "button";
  openButton.className = "project-open-button";
  openButton.innerHTML = `<span class="nav-copy"><span class="nav-title">${project.clc?.pinned ? '<span class="pin-mark" aria-label="Fixado">●</span>' : ""}${escapeHTML(project.name)}</span><span class="nav-preview" title="${escapeHTML(preview)}">${escapeHTML(preview)}</span></span>`;
  openButton.addEventListener("click", async () => {
    await selectProject(project.id);
    await newThread();
  });
  item.append(menuButton, openButton);
  return item;
}

function appendProjectGroup(container, title, projects) {
  if (!projects.length) return;
  const group = document.createElement("section");
  group.className = "project-recency-group";
  group.setAttribute("aria-label", title);
  const heading = document.createElement("h3");
  heading.className = "project-group-title";
  heading.innerHTML = `<span>${escapeHTML(title)}</span><small>${projects.length}</small>`;
  group.appendChild(heading);
  projects.forEach(project => group.appendChild(createProjectRow(project)));
  container.appendChild(group);
}

function renderProjects() {
  closeProjectMenu();
  selectors.projectList.innerHTML = "";
  el("add-project").disabled = false;
  el("add-project").title = "Criar, selecionar ou retomar projetos";
  document.querySelectorAll(".project-actions-grid .suggestion").forEach(button => { button.disabled = !state.activeProject; });
  const allButton = document.createElement("button");
  allButton.type = "button";
  allButton.className = `all-conversations-button ${state.activeProject ? "" : "active"}`;
  allButton.innerHTML = '<span aria-hidden="true">◴</span><span><strong>Todas as conversas</strong><small>Todos os projetos</small></span>';
  allButton.addEventListener("click", clearProjectSelection);
  const fixedList = document.createElement("div");
  fixedList.className = "project-fixed-list";
  fixedList.appendChild(allButton);
  state.projects.filter(project => project.kind === "system").forEach(project => fixedList.appendChild(createProjectRow(project)));
  selectors.projectList.appendChild(fixedList);

  const projectScroller = document.createElement("div");
  projectScroller.className = "project-scroll-list";
  projectScroller.setAttribute("role", "region");
  projectScroller.setAttribute("aria-label", "Projetos por utilização recente");
  selectors.projectList.appendChild(projectScroller);
  const normalProjects = state.projects
    .filter(project => project.kind !== "system")
    .sort((left, right) => projectLastUsedTimestamp(right) - projectLastUsedTimestamp(left)
      || left.name.localeCompare(right.name, "pt-BR", {sensitivity:"base"}));
  if (!normalProjects.length) {
    projectScroller.insertAdjacentHTML("beforeend", '<div class="panel-empty project-list-empty">Nenhum projeto cadastrado.</div>');
    return;
  }
  appendProjectGroup(projectScroller, "Mais recentes", normalProjects.slice(0, RECENT_PROJECT_LIMIT));
  appendProjectGroup(projectScroller, "Antigos", normalProjects.slice(RECENT_PROJECT_LIMIT));
}

async function clearProjectSelection() {
  state.conversationViewGeneration += 1;
  state.threadLoadId = crypto.randomUUID();
  state.activeProject = null;
  state.activeThreadId = null;
  state.activeTurnId = null;
  state.items.clear();
  state.diff = "";
  selectors.title.textContent = "Conversas recentes";
  selectors.projectLabel.textContent = "Todos os projetos • atividade mais recente";
  el("rename-thread").disabled = true;
  el("archive-thread").disabled = true;
  renderProjects();
  renderMessages();
  renderApprovals();
  renderDiff();
  setConversationContextUI();
  updateRunningUI();
  closeMobilePanels();
  await loadThreads();
}

async function addProjectGraphically(fromSetup = false) {
  if (state.identity !== "localhost") return toast("Abra o painel diretamente no computador Linux para escolher uma pasta.", "error");
  try {
    toast("Escolha uma pasta na janela do Linux.");
    const data = await api("/api/projects/pick", {method:"POST", body:JSON.stringify({})});
    const suggested = data.project?.name || "Projeto";
    const name = window.prompt("Nome para este projeto:", suggested);
    if (name?.trim() && name.trim() !== suggested) {
      await api(`/api/projects/${encodeURIComponent(data.project.id)}`, {method:"PATCH", body:JSON.stringify({name:name.trim()})});
    }
    if (fromSetup) await refreshSetupState();
    else {
      await loadProjects();
      await selectProject(data.project.id);
      await loadStatus();
    }
    toast("Projeto adicionado.", "success");
  } catch (error) {
    if (!/cancelada/i.test(error.message)) toast(error.message, "error");
  }
}

function renderProjectManager() {
  const rootSelect = el("project-root-select");
  rootSelect.innerHTML = state.projectRoots.length
    ? state.projectRoots.map(item => `<option value="${escapeHTML(item.path)}" ${item.path === state.projectRoot ? "selected" : ""}>${escapeHTML(item.name)} — ${escapeHTML(item.path)}</option>`).join("")
    : '<option value="">Nenhuma pasta raiz disponível</option>';
  el("project-folder-current").textContent = displayProjectPath(state.projectFolderPath) || "Nenhuma pasta selecionada";
  el("project-folder-up").disabled = !state.projectFolderParent;
  selectors.projectFolderList.innerHTML = state.projectDirectories.length
    ? state.projectDirectories.map(item => `<button type="button" class="project-folder-row" data-project-folder="${escapeHTML(item.path)}"><span class="backup-folder-icon">▱</span><span>${escapeHTML(item.name)}</span><span>›</span></button>`).join("")
    : '<div class="panel-empty">Esta pasta não contém outras pastas. Você pode usar a própria pasta atual.</div>';
  el("project-folder-selection-list").innerHTML = (state.projectSelectedPaths || []).map((path, index) => `<span class="reference-chip"><b>${index === 0 ? "Principal" : "Pasta"}</b> ${escapeHTML(displayProjectPath(path))} <button type="button" data-remove-project-folder="${index}" aria-label="Remover pasta">×</button></span>`).join("");
  const normal = state.projects.filter(project => project.kind !== "system");
  selectors.projectManagerList.innerHTML = normal.length ? normal.map(project => {
    const bridge = state.bridges.projects?.projects?.[project.id] || {};
    const active = state.activeProject?.id === project.id;
    const status = bridge.running ? "Worker em execução" : active ? "Projeto selecionado" : "Salvo e pronto para retomar";
    return `<div class="project-manager-row"><div class="project-manager-copy"><strong>${escapeHTML(project.name)}</strong><small>${escapeHTML(displayProjectPath(project.path))} • ${status}</small></div><button class="${active ? "secondary-button" : "primary-button"}" data-open-project="${escapeHTML(project.id)}">${active ? "Aberto" : "Abrir projeto"}</button></div>`;
  }).join("") : '<div class="inline-notice">Nenhum projeto cadastrado. Crie um novo ou selecione uma pasta existente.</div>';
}

function displayProjectPath(path) {
  const value = String(path || "");
  if (!value) return "";
  const root = state.projectRoots.find(item => value === item.path || value.startsWith(`${item.path}/`));
  if (!root || !String(root.name || "").startsWith("OneDrive:")) return value;
  const remote = String(root.name).slice("OneDrive:".length).trim().split("/").filter(Boolean).join(" / ");
  const relative = value.slice(root.path.length).replace(/^\/+/, "").split("/").filter(Boolean).join(" / ");
  return `OneDrive / ${remote}${relative ? ` / ${relative}` : ""}`;
}

async function refreshProjectManager(root = state.projectRoot, path = "") {
  const query = new URLSearchParams();
  if (root) query.set("root", root);
  if (path) query.set("path", path);
  const projectsPromise = loadProjects();
  const statusPromise = loadStatus();
  const directoryData = await api(`/api/projects/directories?${query}`);
  state.projectRoots = directoryData.roots || [];
  state.projectRoot = directoryData.root || "";
  state.projectFolderPath = directoryData.current || state.projectRoot;
  state.projectFolderParent = directoryData.parent || "";
  state.projectDirectories = directoryData.directories || [];
  renderProjectManager();
  await Promise.all([projectsPromise, statusPromise]);
  renderProjectManager();
}

async function openProjectManager(mode = "manage") {
  closeMobilePanels();
  if (!selectors.projectDialog.open) selectors.projectDialog.showModal();
  const rootSelect = el("project-root-select");
  if (!state.projectRoots.length) {
    rootSelect.innerHTML = '<option value="">Carregando pasta padrão…</option>';
    rootSelect.disabled = true;
  }
  try {
    await refreshProjectManager();
  } finally {
    rootSelect.disabled = false;
  }
  state.projectSelectedPaths = [];
  renderProjectManager();
  if (mode === "create") el("project-create-name").focus();
  else if (mode === "select") await openProjectRootBrowser();
}

async function loadProjectRootBrowser(path = "") {
  selectors.projectRootBrowserList.innerHTML = '<div class="panel-empty">Carregando locais disponíveis…</div>';
  const data = await api(`/api/projects/root-folders?path=${encodeURIComponent(path)}`);
  state.projectRootBrowserPath = data.current || "";
  state.projectRootBrowserParent = data.parent || "";
  el("project-root-browser-current").textContent = state.projectRootBrowserPath || "Locais disponíveis";
  el("project-root-browser-up").disabled = !state.projectRootBrowserPath;
  el("project-root-browser-select").disabled = !state.projectRootBrowserPath;
  el("project-root-browser-create").disabled = !state.projectRootBrowserPath;
  const oneDriveRow = !state.projectRootBrowserPath && data.onedrive?.available
    ? '<button type="button" class="backup-folder-row" data-project-root-onedrive><span class="backup-folder-icon">☁</span><span><strong>OneDrive</strong><small>Escolher uma pasta na nuvem</small></span><span>›</span></button>'
    : "";
  const localRows = (data.folders || []).map(folder => `<button type="button" class="backup-folder-row" data-project-root-folder="${escapeHTML(folder.path)}"><span class="backup-folder-icon">▱</span><span><strong>${escapeHTML(folder.name)}</strong><small>${escapeHTML(folder.path)}</small></span><span>›</span></button>`).join("");
  selectors.projectRootBrowserList.innerHTML = oneDriveRow || localRows
    ? oneDriveRow + localRows
    : '<div class="panel-empty">Esta pasta não contém outras pastas.</div>';
}

async function openProjectRootBrowser() {
  if (!selectors.projectRootDialog.open) selectors.projectRootDialog.showModal();
  await loadProjectRootBrowser("");
}

async function createProjectRootBrowserFolder() {
  const name = el("project-root-browser-new-name").value.trim();
  if (!state.projectRootBrowserPath) return toast("Abra uma pasta antes de criar outra.", "error");
  if (!name) return toast("Informe o nome da nova pasta.", "error");
  const data = await api("/api/projects/root-folders", {method:"POST", body:JSON.stringify({parent:state.projectRootBrowserPath, name})});
  el("project-root-browser-new-name").value = "";
  await loadProjectRootBrowser(data.folder.path);
  toast("Pasta criada. Agora você pode usá-la como raiz.", "success");
}

async function selectProjectRootBrowserFolder() {
  if (!state.projectRootBrowserPath) return toast("Selecione uma pasta no Explorer.", "error");
  const data = await api("/api/projects/roots", {method:"POST", body:JSON.stringify({path:state.projectRootBrowserPath})});
  selectors.projectRootDialog.close();
  await refreshProjectManager(data.root.path);
  toast("Pasta raiz selecionada.", "success");
}

async function createProjectFolder() {
  const name = el("project-create-name").value.trim();
  if (!name) return toast("Informe o nome do projeto.", "error");
  if (!state.projectRoot) return toast("Escolha uma pasta raiz.", "error");
  const data = await api("/api/projects/create-folder", {method:"POST", body:JSON.stringify({name, root:state.projectRoot})});
  el("project-create-name").value = "";
  await loadProjects();
  await selectProject(data.project.id);
  selectors.projectDialog.close();
  toast("Projeto criado e aberto.", "success");
}

async function selectExistingProjectFolder() {
  const paths = [...new Set([...(state.projectSelectedPaths || []), state.projectFolderPath].filter(Boolean))];
  const path = paths[0] || "";
  const fallback = path ? path.split("/").filter(Boolean).at(-1) : "Projeto";
  const name = el("project-folder-name").value.trim() || fallback;
  if (!path) return toast("Selecione uma pasta.", "error");
  const data = await api("/api/projects", {method:"POST", body:JSON.stringify({name, path})});
  if (paths.length > 1) await api(`/api/projects/${encodeURIComponent(data.project.id)}/metadata`, {method:"PATCH", body:JSON.stringify({main_path:path, paths})});
  el("project-folder-name").value = "";
  state.projectSelectedPaths = [];
  await loadProjects();
  await selectProject(data.project.id);
  selectors.projectDialog.close();
  toast("Pasta adicionada e projeto aberto.", "success");
}

async function renameProject(projectId) {
  const project = state.projects.find(item => item.id === projectId) || state.setup.data?.projects?.find(item => item.id === projectId);
  if (!project) return;
  const name = window.prompt("Novo nome do projeto:", project.name);
  if (!name?.trim()) return;
  try {
    await api(`/api/projects/${encodeURIComponent(projectId)}`, {method:"PATCH", body:JSON.stringify({name:name.trim()})});
    if (state.setup.active) await refreshSetupState();
    else await loadProjects();
    toast("Projeto renomeado.", "success");
  } catch (error) { toast(error.message, "error"); }
}

async function toggleProjectPinned(project) {
  try {
    await api(`/api/projects/${encodeURIComponent(project.id)}/metadata`, {
      method:"PATCH",
      body:JSON.stringify({pinned:!Boolean(project.clc?.pinned)}),
    });
    await loadProjects();
    toast(project.clc?.pinned ? "Projeto desafixado." : "Projeto fixado.", "success");
  } catch (error) { toast(error.message, "error"); }
}

async function archiveProjectThreads(project) {
  if (!window.confirm(`Arquivar todas as conversas ativas de “${project.name}”? Os arquivos do projeto não serão alterados.`)) return;
  try {
    const result = await api(`/api/projects/${encodeURIComponent(project.id)}/archive-threads`, {method:"POST"});
    if (state.activeProject?.id === project.id) {
      newThread();
      await loadThreads();
    }
    toast(`${result.archived || 0} conversa(s) arquivada(s).`, "success");
  } catch (error) { toast(error.message, "error"); }
}

async function refreshAfterProjectRemoval(projectId) {
  if (state.activeProject?.id === projectId) state.activeProject = null;
  if (state.setup.active) await refreshSetupState();
  else {
    await loadProjects();
    if (state.projects.length) await selectProject(state.projects[0].id);
  }
}

async function deleteProjectGraphically(projectId) {
  if (!window.confirm("Remover este projeto da interface? Os arquivos da pasta não serão apagados.")) return;
  try {
    await api(`/api/projects/${encodeURIComponent(projectId)}`, {method:"DELETE"});
    await refreshAfterProjectRemoval(projectId);
    toast("Projeto removido.", "success");
  } catch (error) { toast(error.message, "error"); }
}

async function deleteProjectFiles(project) {
  const confirmation = window.prompt(
    `Esta ação exclui permanentemente a pasta “${project.path}” e remove o projeto da interface. Se houver sincronização, a exclusão poderá chegar à nuvem.\n\nDigite exatamente o nome do projeto para confirmar:`,
  );
  if (confirmation === null) return;
  if (confirmation.trim() !== project.name) return toast("O nome digitado não corresponde ao projeto.", "error");
  try {
    await api(`/api/projects/${encodeURIComponent(project.id)}/delete-files`, {
      method:"POST",
      body:JSON.stringify({confirmation:confirmation.trim()}),
    });
    await refreshAfterProjectRemoval(project.id);
    toast("Pasta e projeto excluídos permanentemente.", "success");
  } catch (error) { toast(error.message, "error"); }
}

function emptyToolProfile() {
  return {skills: [], apps: [], mcp_servers: [], browser: false, desktop: false, system_admin: false, automatic: true};
}

function normalizeToolProfile(value) {
  const source = value && typeof value === "object" ? value : {};
  const uniqueBy = (items, key) => {
    const seen = new Set();
    return (Array.isArray(items) ? items : []).filter(item => {
      const id = typeof item === "string" ? item : item?.[key];
      if (!id || seen.has(id)) return false;
      seen.add(id);
      return true;
    });
  };
  return {
    skills: uniqueBy(source.skills, "path").map(item => typeof item === "string" ? {name:item.split("/").pop(), path:item} : item),
    apps: uniqueBy(source.apps, "id").map(item => typeof item === "string" ? {id:item, name:item, slug:item} : item),
    mcp_servers: [...new Set((source.mcp_servers || []).filter(Boolean))],
    browser: Boolean(source.browser),
    desktop: Boolean(source.desktop),
    system_admin: Boolean(source.system_admin),
    automatic: source.automatic !== false,
  };
}

function toolProfileCount(profile = state.toolProfile) {
  return profile.skills.length + profile.apps.length + profile.mcp_servers.length + Number(profile.browser) + Number(profile.desktop) + Number(profile.system_admin);
}

function renderToolProfileChip() {
  const count = toolProfileCount();
  selectors.toolCount.textContent = count ? String(count) : "A";
  selectors.composerTools.textContent = count ? `${count} ferramenta${count === 1 ? "" : "s"} • Auto` : "Ferramentas • Auto";
  const names = [];
  if (state.toolProfile.system_admin) names.push("Administração SASOCQ");
  if (state.toolProfile.browser) names.push("Navegador");
  if (state.toolProfile.desktop) names.push("Desktop");
  names.push(...state.toolProfile.skills.map(item => item.name || item.path));
  names.push(...state.toolProfile.apps.map(item => item.name || item.id));
  names.push(...state.toolProfile.mcp_servers);
  if (state.toolProfile.automatic) names.unshift("Seleção automática ativa");
  selectors.composerTools.title = names.join(", ");
  selectors.composerTools.classList.toggle("active", state.toolProfile.automatic || count > 0);
}

async function loadToolProfile(threadId = state.activeThreadId) {
  if (!state.activeProject) {
    state.toolProfile = emptyToolProfile();
    renderToolProfileChip();
    return state.toolProfile;
  }
  try {
    const query = new URLSearchParams({project_id:state.activeProject.id});
    if (threadId) query.set("thread_id", threadId);
    const data = await api(`/api/tool-profile?${query}`);
    state.toolProfile = normalizeToolProfile(data.profile);
  } catch (error) {
    state.toolProfile = emptyToolProfile();
    addActivity("Ferramentas", error.message, "error");
  }
  renderToolProfileChip();
  return state.toolProfile;
}

async function selectProject(projectId) {
  const project = state.projects.find(item => item.id === projectId);
  if (!project) return;
  if (project.kind !== "system") markProjectUsed(project.id);
  state.conversationViewGeneration += 1;
  state.threadLoadId = crypto.randomUUID();
  state.activeProject = project;
  state.composerPreferences = composerPreferencesForProject(project);
  state.activeThreadId = null;
  state.activeTurnId = null;
  state.items.clear();
  state.diff = "";
  selectors.projectLabel.textContent = project.kind === "system"
    ? "Control Plane permanente • host, servidor, recursos e jogos"
    : `Worker isolado • autonomia na VM e em sasocq.com • ${project.path}`;
  selectors.title.textContent = "Nova conversa";
  el("rename-thread").disabled = true;
  el("archive-thread").disabled = true;
  renderProjects();
  renderMessages();
  renderApprovals();
  renderDiff();
  syncActiveBridgeUI();
  setConversationContextUI();
  closeMobilePanels();
  publishCachedThreadSummaries([project]);
  updateRunningUI();
  void Promise.all([loadModels(), loadAccount(), loadThreads()]).then(() => {
    if (!state.activeThreadId && state.activeProject?.id === projectId) return loadToolProfile(null);
    return null;
  });
}

async function loadThreads() {
  const generation = ++state.threadLoadGeneration;
  const projects = state.activeProject ? [state.activeProject] : [...state.projects];
  const results = projects.map(project => state.projectThreads.get(project.id) || []);
  const publish = () => {
    if (generation !== state.threadLoadGeneration) return false;
    state.threads = results.flat().sort((left, right) => threadActivityTimestamp(right) - threadActivityTimestamp(left));
    renderProjects();
    renderThreads();
    renderOtherConversationApprovals();
    return true;
  };
  publish();

  // Limit concurrent worker starts. Opening every project bridge at once made
  // the mobile page appear frozen and increased the chance of request timeouts.
  let nextIndex = 0;
  const worker = async () => {
    while (generation === state.threadLoadGeneration) {
      const index = nextIndex++;
      if (index >= projects.length) return;
      const project = projects[index];
      const bridge = project.kind === "system" ? state.bridges.system : state.bridges.projects;
      if (!bridge?.initialized) continue;
      try {
        const requestLimit = PROJECT_CONVERSATION_LIMIT;
        const data = await api(`/api/threads?project_id=${encodeURIComponent(project.id)}&limit=${requestLimit}`);
        if (generation !== state.threadLoadGeneration) return;
        const threads = (data.data || []).map(thread => {
          const terminal = state.threadTerminalStatuses.get(thread.id);
          return {
            ...thread,
            status:terminal ? {type:terminal.status, message:terminal.message, localTerminal:true} : thread.status,
            _projectId:project.id,
            _projectName:project.name,
            _projectKind:project.kind,
          };
        });
        state.projectThreads.set(project.id, threads);
        results[index] = threads;
        state.projectActivity.set(project.id, threads.reduce((latest, thread) => Math.max(latest, threadActivityTimestamp(thread)), 0));
        publish();
      } catch (error) {
        addActivity(`Conversas • ${project.name}`, error.message, "error");
      }
    }
  };
  await Promise.all(Array.from({length:Math.min(3, projects.length)}, worker));
  persistThreadSummaryCache();
  publish();
  return state.threads;
}

function persistThreadSummaryCache() {
  try {
    const compact = {};
    for (const [projectId, threads] of state.projectThreads) {
      compact[projectId] = threads.slice(0, 24).map(thread => ({
        id:thread.id, name:thread.name, preview:String(thread.preview || "").slice(0, 600),
        createdAt:thread.createdAt, updatedAt:thread.updatedAt, startedAt:thread.startedAt,
        completedAt:thread.completedAt, finishedAt:thread.finishedAt, status:thread.status,
        archived:thread.archived, clc:thread.clc, _projectId:thread._projectId,
        _projectName:thread._projectName, _projectKind:thread._projectKind,
      }));
    }
    localStorage.setItem(THREAD_SUMMARY_CACHE_KEY, JSON.stringify(compact));
  } catch { /* Live navigation remains usable if browser storage is unavailable. */ }
}

function publishCachedThreadSummaries(projects = state.activeProject ? [state.activeProject] : state.projects) {
  state.threads = projects.flatMap(project => state.projectThreads.get(project.id) || [])
    .sort((left, right) => threadActivityTimestamp(right) - threadActivityTimestamp(left));
  renderProjects();
  renderThreads();
}

function threadActivityTimestamp(thread) {
  const value = thread.completedAt || thread.finishedAt || thread.updatedAt || thread.startedAt || thread.createdAt || 0;
  if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function threadStatus(thread) {
  const status = thread.status || {};
  if (status.type === "active" && threadWaitingKind(status) && pendingUserActionForThread(thread.id)) return "waiting";
  if (status.type === "active") return "active";
  if (["failed", "error", "stopped"].includes(String(status.type || "").toLowerCase())) return "failed";
  if (thread?.clc?.awaiting_user_action) return "waiting";
  return "";
}

function threadUserActionLabel(thread) {
  return String(thread?.clc?.awaiting_user_action_label || "Responder ao Codex");
}

function setThreadAwaitingUserAction(threadId, waiting, label = "") {
  if (!threadId) return;
  const seen = new Set();
  const apply = thread => {
    if (!thread || thread.id !== threadId || seen.has(thread)) return;
    seen.add(thread);
    thread.clc = {...(thread.clc || {}), awaiting_user_action:Boolean(waiting), awaiting_user_action_label:waiting ? String(label || "Responder ao Codex") : ""};
  };
  for (const threads of state.projectThreads.values()) for (const thread of threads) apply(thread);
  for (const thread of state.threads) apply(thread);
  apply(state.threadDetails.get(threadId));
  persistThreadSummaryCache();
  renderThreads();
}

function threadWaitingKind(value) {
  const status = value?.status || value || {};
  const flags = Array.isArray(status.activeFlags) ? status.activeFlags : [];
  if (flags.includes("waitingOnUserInput")) return "input";
  if (flags.includes("waitingOnApproval")) return "approval";
  return "";
}

function requestWaitingKind(method, params = {}) {
  if (method === "item/tool/requestUserInput" && params.isBlocking === false) return "";
  if (["item/tool/requestUserInput", "mcpServer/elicitation/request"].includes(method)) return "input";
  if (["item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"].includes(method)) return "approval";
  return "";
}

function userActionLabel(approval) {
  const method = String(approval?.method || "");
  const params = approval?.params || {};
  const actionText = [params.reason, params.message, params.toolName, params.tool, commandText(params.command)]
    .filter(Boolean).join(" ");
  if (/login|log in|sign in|autentic|autoriza[cç][aã]o|authentication|authorization/i.test(actionText)) return "Concluir autenticação";
  if (/linux[_ -]?desktop|desktop_(?:click|type|press)|mouse|teclado|keyboard|controlar (?:a )?(?:tela|interface)/i.test(actionText)) return "Controlar interface";
  if (/playwright|browser_(?:click|type|press|navigate)|navegador/i.test(actionText)) return "Controlar navegador";
  if (/\b(?:delete|remove|destroy|erase|excluir|apagar|remover|destrut)/i.test(actionText)) return "Confirmar exclusão";
  if (/\b(?:install|instalar|instala[cç][aã]o)\b/i.test(actionText)) return "Instalar componente";
  if (/\b(?:restart|reboot|reiniciar|reinicializar)\b/i.test(actionText)) return "Reiniciar componente";
  if (/\b(?:network|internet|rede)\b/i.test(actionText) && method === "item/permissions/requestApproval") return "Liberar acesso à rede";
  if (/\b(?:send|publish|purchase|payment|enviar|publicar|comprar|pagamento)\b/i.test(actionText)) return "Confirmar ação externa";
  if (method === "item/commandExecution/requestApproval") return "Aprovar comando";
  if (method === "item/fileChange/requestApproval") return "Aprovar alteração";
  if (method === "item/permissions/requestApproval") return "Conceder permissão";
  if (method === "item/tool/requestUserInput") {
    const questions = Array.isArray(params.questions) ? params.questions : [];
    if (questions.some(question => question?.isSecret)) return "Informar dado protegido";
    if (questions.some(question => Array.isArray(question?.options) && question.options.length)) return "Escolher uma opção";
    return "Responder pergunta";
  }
  if (method === "mcpServer/elicitation/request") {
    if (/confirm|approve|allow|permit|autorize|autoriza|aprovar|permitir/i.test(actionText)) return "Confirmar ação";
    return "Responder à ferramenta";
  }
  return "Responder ao Codex";
}

function persistThreadTerminalStatuses() {
  try { localStorage.setItem(THREAD_TERMINAL_STATUS_KEY, JSON.stringify(Object.fromEntries(state.threadTerminalStatuses))); }
  catch { /* The live status still works if browser storage is unavailable. */ }
}

function clearThreadTerminalStatus(threadId) {
  if (!threadId || !state.threadTerminalStatuses.delete(threadId)) return;
  persistThreadTerminalStatuses();
}

function recordThreadTerminalStatus(threadId, message) {
  if (!threadId) return;
  state.threadTerminalStatuses.set(threadId, {status:"failed", message, at:Date.now()});
  persistThreadTerminalStatuses();
  updateThreadRuntimeStatus(threadId, {type:"failed", message, localTerminal:true});
}

function bridgeMatchesThread(rawWorkspace, thread) {
  if (!thread) return false;
  if (rawWorkspace === "system") return thread._projectKind === "system" || thread._projectId === "system-control";
  if (rawWorkspace.startsWith("project:")) return thread._projectId === rawWorkspace.slice("project:".length);
  if (rawWorkspace === "projects") return thread._projectKind !== "system";
  return false;
}

function processEndedMessage(detail = "") {
  const suffix = String(detail || "").trim();
  return `O processo desta conversa foi encerrado antes de entregar uma resposta. O histórico foi preservado; envie uma nova mensagem para tentar novamente.${suffix ? ` Detalhe: ${suffix}` : ""}`;
}

function markBridgeExecutionsFailed(rawWorkspace, detail = "") {
  const message = processEndedMessage(detail);
  const affected = new Set();
  for (const threads of state.projectThreads.values()) {
    for (const thread of threads) {
      if (bridgeMatchesThread(rawWorkspace, thread) && threadStatus(thread) === "active") affected.add(thread.id);
    }
  }
  if (state.activeThreadId && state.activeTurnId && rawWorkspace === activeEventWorkspace()) affected.add(state.activeThreadId);
  for (const threadId of affected) recordThreadTerminalStatus(threadId, message);
  if (!affected.has(state.activeThreadId)) return;
  state.turnSubmissionPending = false;
  state.activeTurnId = null;
  clearLocalActivity();
  const errorId = `process-ended-${state.activeThreadId}`;
  state.items.set(errorId, {id:errorId, type:"error", message});
  setStatus("error", "Processo encerrado");
  updateRunningUI();
  toast("O processo da conversa foi encerrado. O histórico foi preservado.", "error");
}

function updateThreadRuntimeStatus(threadId, status) {
  if (!threadId || !status) return false;
  let changed = false;
  const seen = new Set();
  const apply = thread => {
    if (!thread || thread.id !== threadId || seen.has(thread)) return;
    seen.add(thread);
    thread.status = status;
    changed = true;
  };
  for (const threads of state.projectThreads.values()) {
    for (const thread of threads) apply(thread);
  }
  for (const thread of state.threads) apply(thread);
  if (changed) {
    persistThreadSummaryCache();
    renderThreads();
  }
  return changed;
}

function isGeneratedConversationEnvelopeText(value) {
  const text = String(value || "").trim();
  const hasRequestMarker = text.includes("Solicitação atual:") || text.includes("Solicitação do usuário:");
  const hasGeneratedPreface = /^\$clc-[^\s]+/i.test(text)
    || text.includes("Use preferencialmente os servidores MCP associados a esta conversa:")
    || text.includes("A administração SASOCQ está associada a esta conversa.")
    || text.includes("Referências selecionadas pelo operador:");
  return hasRequestMarker && hasGeneratedPreface;
}

function visibleConversationText(value) {
  let text = String(value || "").trim();
  if (isGeneratedConversationEnvelopeText(text)) {
    const requestMarkers = ["Solicitação atual:", "Solicitação do usuário:"];
    const selectedMarker = requestMarkers
      .map(marker => ({marker, index:text.lastIndexOf(marker)}))
      .filter(candidate => candidate.index >= 0)
      .sort((left, right) => right.index - left.index)[0];
    if (selectedMarker) text = text.slice(selectedMarker.index + selectedMarker.marker.length).trim();
  }
  const referencesMarker = "Referências selecionadas pelo operador:";
  const referencesIndex = text.indexOf(referencesMarker);
  if (referencesIndex >= 0) text = text.slice(0, referencesIndex).trim();
  text = text.replace(/^Direção antecipada da fila:\s*/i, "").trim();
  text = text.replace(/^\$clc-[^\s]+\s*/i, "").trim();
  return text;
}

function isUserMessageItem(item) {
  return ["userMessage", "user_message"].includes(item?.type);
}

function isTechnicalUserEnvelope(item) {
  if (!isUserMessageItem(item)) return false;
  return isGeneratedConversationEnvelopeText(extractContent(item));
}

function normalizedMessageText(value) {
  return visibleConversationText(value).replace(/\s+/g, " ").trim().toLocaleLowerCase("pt-BR");
}

function compactReasoningPreview(value) {
  const pieces = String(value || "")
    .split(/\n+|(?<=[.!?])\s+/)
    .map(part => part.replace(/\s+/g, " ").trim())
    .filter(Boolean);
  const unique = pieces.filter((part, index) => pieces.findIndex(candidate => candidate.toLocaleLowerCase("pt-BR") === part.toLocaleLowerCase("pt-BR")) === index);
  const preview = unique.slice(0, 2).join(" ") || "Preparando a próxima etapa…";
  return preview.length > 180 ? `${preview.slice(0, 177).trimEnd()}…` : preview;
}

function conversationItemsForDisplay(items) {
  const ordinaryUserTexts = new Set(items
    .filter(item => isUserMessageItem(item) && !isTechnicalUserEnvelope(item))
    .map(item => normalizedMessageText(extractContent(item)))
    .filter(Boolean));
  const seenReasoning = new Set();
  const compact = [];
  let newerTechnicalActivityCompleted = false;
  for (const item of items) {
    const technicalActivity = ["commandExecution", "fileChange", "mcpToolCall"].includes(item.type);
    const handledFailure = technicalActivity
      && item.status === "failed"
      && newerTechnicalActivityCompleted;
    const displayItem = handledFailure ? {...item, _failureHandled:true} : item;
    if (isTechnicalUserEnvelope(item)) {
      const clean = normalizedMessageText(extractContent(item));
      if (clean && ordinaryUserTexts.has(clean)) continue;
    }
    if (item.type === "reasoning") {
      const key = String(extractContent(item) || "").replace(/\s+/g, " ").trim().toLocaleLowerCase("pt-BR");
      if (key && seenReasoning.has(key)) continue;
      if (key) seenReasoning.add(key);
      if (compact.at(-1)?.type === "reasoning") compact.pop();
    }
    compact.push(displayItem);
    if (technicalActivity && item.status === "completed") newerTechnicalActivityCompleted = true;
  }
  return compact;
}

function isCodexMessageItem(item) {
  return ["agentMessage", "assistantMessage", "agent_message", "plan"].includes(item?.type);
}

function isBrowserToolItem(item) {
  if (item?.type !== "mcpToolCall") return false;
  const server = String(item.server || "").toLowerCase();
  const tool = String(item.tool || "").toLowerCase();
  return server === "playwright" || server.includes("browser") || tool.startsWith("browser_");
}

function isAndroidToolItem(item) {
  if (item?.type === "mcpToolCall") {
    const server = String(item.server || "").toLowerCase();
    const tool = String(item.tool || "").toLowerCase();
    return server.includes("android") || server.includes("appium") || tool.startsWith("android_");
  }
  if (item?.type !== "commandExecution") return false;
  const command = commandText(item.command).toLowerCase();
  return command.includes("sasocq-androidctl")
    || command.includes("192.168.240.112:5555")
    || (command.includes("127.0.0.1:4723") && command.includes("/session"));
}

function groupAndroidActivities(items) {
  const groups = new Map();
  let segment = 0;
  items.forEach((item, index) => {
    if (isCodexMessageItem(item)) {
      segment += 1;
      return;
    }
    if (!isAndroidToolItem(item)) return;
    const entries = groups.get(segment) || [];
    entries.push({item, index});
    groups.set(segment, entries);
  });
  const replacements = new Map();
  const groupedIndexes = new Set();
  for (const entries of groups.values()) {
    replacements.set(entries[0].index, {
      id:`android-session-${entries.at(-1).item.id || entries[0].index}`,
      type:"androidActivityGroup",
      items:entries.map(entry => entry.item),
    });
    entries.slice(1).forEach(entry => groupedIndexes.add(entry.index));
  }
  return items.flatMap((item, index) => {
    if (replacements.has(index)) return [replacements.get(index)];
    return groupedIndexes.has(index) ? [] : [item];
  });
}

function groupBrowserActivities(items) {
  const groups = new Map();
  let segment = 0;
  items.forEach((item, index) => {
    if (isCodexMessageItem(item)) {
      segment += 1;
      return;
    }
    if (!isBrowserToolItem(item)) return;
    const entries = groups.get(segment) || [];
    entries.push({item, index});
    groups.set(segment, entries);
  });
  const replacements = new Map();
  const groupedIndexes = new Set();
  for (const entries of groups.values()) {
    replacements.set(entries[0].index, {
      id:`browser-session-${entries.at(-1).item.id || entries[0].index}`,
      type:"browserActivityGroup",
      items:entries.map(entry => entry.item),
    });
    entries.slice(1).forEach(entry => groupedIndexes.add(entry.index));
  }
  return items.flatMap((item, index) => {
    if (replacements.has(index)) return [replacements.get(index)];
    return groupedIndexes.has(index) ? [] : [item];
  });
}

function groupTechnicalActivities(items) {
  const technicalTypes = new Set(["commandExecution", "fileChange", "mcpToolCall"]);
  const groups = new Map();
  let segment = 0;
  items.forEach((item, index) => {
    if (isCodexMessageItem(item)) {
      segment += 1;
      return;
    }
    if (!technicalTypes.has(item?.type)) return;
    const entries = groups.get(segment) || [];
    entries.push({item, index});
    groups.set(segment, entries);
  });
  const replacements = new Map();
  const groupedIndexes = new Set();
  for (const entries of groups.values()) {
    if (entries.length < 2) continue;
    const oldestItem = entries.at(-1).item;
    replacements.set(entries[0].index, {
      id:`technical-activity-group-${oldestItem.id || entries[0].index}`,
      type:"technicalActivityGroup",
      items:entries.map(entry => entry.item),
    });
    entries.slice(1).forEach(entry => groupedIndexes.add(entry.index));
  }
  const grouped = items.flatMap((item, index) => {
    if (replacements.has(index)) return [replacements.get(index)];
    return groupedIndexes.has(index) ? [] : [item];
  });
  return grouped.reduce((compacted, item) => {
    if (item.type === "reasoning" && compacted.at(-1)?.type === "reasoning") compacted.pop();
    compacted.push(item);
    return compacted;
  }, []);
}

const CONVERSATION_TITLE_MAX_WORDS = 6;
const CONVERSATION_TITLE_MAX_LENGTH = 52;

function compactContextTitle(value, maxWords = CONVERSATION_TITLE_MAX_WORDS, maxLength = CONVERSATION_TITLE_MAX_LENGTH) {
  let clean = String(value || "")
    .replace(/https?:\/\/\S+/gi, "")
    .replace(/[`*_#>]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/^[\s:;,.!?–—-]+|[\s:;,.!?–—-]+$/g, "")
    .trim();
  let words = clean.split(/\s+/).filter(Boolean);
  if (words.length > maxWords) words = words.slice(0, maxWords);
  while (words.length > 2 && /^(?:a|o|as|os|de|da|do|das|dos|e|em|com|para)$/i.test(words.at(-1))) words.pop();
  clean = words.join(" ");
  if (clean.length > maxLength) clean = clean.slice(0, maxLength + 1).replace(/\s+\S*$/, "").trim();
  return clean.replace(/[,:;.!?–—-]+$/g, "").trim();
}

function actionConversationTitle(noun, rawTarget) {
  let target = String(rawTarget || "")
    .split(/[.!?;]|\s+(?:para que|porque|pois|quando|assim como|de acordo com|sem que|em vez de|ao invés de)\s+/i, 1)[0]
    .replace(/\s+(?:com|usando)\s+(?:poucas palavras|um título curto|títulos curtos).*$/i, "")
    .replace(/^(?:que\s+)?/i, "")
    .trim();
  if (!target) return "";
  const article = target.match(/^(o|a|os|as|um|uma)\s+(.+)$/i);
  let connector = "de";
  if (article) {
    const contractions = {o:"do", a:"da", os:"dos", as:"das", um:"de", uma:"de"};
    connector = contractions[article[1].toLocaleLowerCase("pt-BR")] || "de";
    target = article[2];
  }
  return compactContextTitle(`${noun} ${connector} ${target}`);
}

function contextualConversationTitle(value, thread = {}) {
  const request = visibleConversationText(value).replace(/\s+/g, " ").trim();
  const normalized = request.toLocaleLowerCase("pt-BR");
  const project = thread._projectName || state.activeProject?.name || "Projeto";
  const systemContext = thread._projectKind === "system" || state.activeProject?.kind === "system";
  if (!request || /^(sim|não|nao|ok|certo|pode|confirmo|continue|continuar|prossiga|isso)$/i.test(request)) {
    return systemContext ? "Administração do Sistema" : compactContextTitle(`Continuação de ${project}`);
  }
  const contextualRules = [
    {all:[/conversa/, /(mesmo nome|nome repetid|t[ií]tulo repetid)/], title:"Títulos repetidos nas conversas"},
    {all:[/t[ií]tulo/, /context/], any:[/mensagem/, /conversa/, /pedido/], title:"Títulos curtos e contextuais"},
    {all:[/(pesquis|busc)/, /conversa/], title:"Busca de conversas"},
    {all:[/erro/, /conversa/, /whatsapp/], title:"Erro na conversa do WhatsApp"},
    {all:[/card/, /navega[cç][aã]o ao vivo/, /(print|[uú]ltima p[aá]gina)/], title:"Prévia da navegação ao vivo"},
    {all:[/chrom/, /playwright/], title:"Chrome com Playwright"},
    {all:[/playwright/, /pedir/, /toda hora/], title:"Permissão persistente do Playwright"},
    {all:[/play store/, /waydroid/], title:"Play Store no Waydroid"},
    {all:[/conversa/, /interromp/], title:"Interrupções nas conversas"},
    {all:[/conversa/, /janela/], title:"Janelas isoladas por conversa"},
    {all:[/conex[aã]o remota/, /resolu[cç][aã]o/], title:"Resolução da conexão remota"},
    {all:[/dois toques/, /(deslizar|rolagem|navega[cç][aã]o)/], title:"Rolagem com dois toques"},
    {all:[/orientar agora/, /n[aã]o est[aá] funcionando/], title:"Correção do Orientar agora"},
    {all:[/(atualiza[cç][aã]o|atualiza[cç][oõ]es)/, /(indicador|[ií]cone|pendente)/], title:"Indicador de atualizações"},
    {all:[/conclu[ií]d/, /compact/], any:[/caixa/, /cart[aã]o/, /atividade/], title:"Atividades concluídas compactas"},
    {all:[/anex/, /(andamento|processamento|execu[cç][aã]o)/], title:"Anexos durante a execução"},
    {all:[/orienta/, /fila/], title:"Controles de fila"},
    {all:[/conversa/], any:[/pulando/, /na frente/, /antiga/, /todas as conversas/], title:"Navegação entre conversas"},
  ];
  for (const rule of contextualRules) {
    if (rule.all.every(pattern => pattern.test(normalized)) && (!rule.any || rule.any.some(pattern => pattern.test(normalized)))) return rule.title;
  }
  const cleanedRequest = request
    .replace(/^(?:por favor,?\s*)?(?:(?:eu\s+)?(?:quero|gostaria|preciso|necessito)\s+que|voc[eê]\s+pode|poderia|pode)\s+/i, "")
    .trim();
  const actions = [
    {pattern:/^(?:corrigir|corrija|corrigindo|consertar|conserte|reparar|repare)\s+(.+)/i, noun:"Correção"},
    {pattern:/^(?:melhor|melhorar|melhore|melhora|otimizar|otimize)\s+(.+)/i, noun:"Melhoria"},
    {pattern:/^(?:ajustar|ajuste|alterar|altere)\s+(.+)/i, noun:"Ajuste"},
    {pattern:/^(?:compactar|compacte|compactando)\s+(.+)/i, noun:"Compactação"},
    {pattern:/^(?:criar|crie|montar|monte|desenvolver|desenvolva)\s+(.+)/i, noun:"Criação"},
    {pattern:/^(?:adicionar|adicione|incluir|inclua)\s+(.+)/i, noun:"Adição"},
    {pattern:/^(?:instalar|instale)\s+(.+)/i, noun:"Instalação"},
    {pattern:/^(?:atualizar|atualize)\s+(.+)/i, noun:"Atualização"},
    {pattern:/^(?:configurar|configure)\s+(.+)/i, noun:"Configuração"},
    {pattern:/^(?:remover|remova|excluir|exclua)\s+(.+)/i, noun:"Remoção"},
    {pattern:/^(?:validar|valide|testar|teste)\s+(.+)/i, noun:"Validação"},
    {pattern:/^(?:analisar|analise|investigar|investigue)\s+(.+)/i, noun:"Análise"},
  ];
  for (const action of actions) {
    const match = cleanedRequest.match(action.pattern);
    if (!match?.[1]) continue;
    const title = actionConversationTitle(action.noun, match[1]);
    if (title) return title;
  }
  const broken = cleanedRequest.match(/^((?:o|a|os|as)\s+.+?)\s+(?:não|nao)\s+(?:está|esta|estão|estao)?\s*(?:funcionando|abrindo|carregando|aparecendo|respondendo)/i);
  if (broken?.[1]) return actionConversationTitle("Correção", broken[1]);
  const topic = cleanedRequest
    .split(/[.!?;]|\s+(?:porque|pois|assim como|de acordo com|para que)\s+/i, 1)[0]
    .replace(/^(?:como|qual|quais|onde|quando|por que|porque)\s+/i, "")
    .replace(/\b(?:precisa|precisam|deve|devem|pode|podem)\s+(?:ser|ficar|ter)?\s*/gi, "")
    .trim();
  return compactContextTitle(topic || request);
}

function conversationTitle(thread) {
  const rawTitle = visibleConversationText(thread?.name || thread?.preview || "").replace(/\s+/g, " ").trim();
  const durableTitle = String(thread?.clc?.title || "").replace(/\s+/g, " ").trim();
  let title = thread?.clc?.title_source === "manual" && durableTitle
    ? durableTitle
    : durableTitle ? compactContextTitle(durableTitle) : contextualConversationTitle(rawTitle, thread);
  title = title
    .replace(/\bpc\b/gi, "PC")
    .replace(/\bchat\s*gpt\b/gi, "ChatGPT")
    .replace(/\bdex\b/gi, "Dex")
    .replace(/\bsasocq\b/gi, "SASOCQ")
    .replace(/\bkvm\b/gi, "KVM")
    .replace(/\bandroid\b/gi, "Android")
    .replace(/\bsteam\b/gi, "Steam")
    .replace(/\bpostgresql\b/gi, "PostgreSQL");
  title = title
    .replace(/\bplaywright\b/gi, "Playwright")
    .replace(/\bwaydroid\b/gi, "Waydroid")
    .replace(/\bchrome\b/gi, "Chrome")
    .replace(/\bwhatsapp\b/gi, "WhatsApp");
  if (title) title = title.charAt(0).toLocaleUpperCase("pt-BR") + title.slice(1);
  return title || "Conversa sem título";
}

function normalizeThreadSearchText(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase("pt-BR");
}

function threadSearchSpecificity(value, query) {
  const text = normalizeThreadSearchText(value);
  if (!text || !query) return 9;
  if (text === query) return 0;
  if (text.startsWith(query)) return 1;
  if (text.includes(` ${query}`)) return 2;
  return text.includes(query) ? 3 : 9;
}

function threadSearchRank(thread, query) {
  const title = conversationTitle(thread);
  const titleSpecificity = Math.min(
    threadSearchSpecificity(title, query),
    threadSearchSpecificity(thread.name, query),
  );
  if (titleSpecificity < 9) return {tier:0, specificity:titleSpecificity};

  const request = visibleConversationText(thread.preview);
  const requestSpecificity = threadSearchSpecificity(request, query);
  if (requestSpecificity < 9) return {tier:1, specificity:requestSpecificity};

  const kind = String(thread.search?.kind || "");
  const snippetSpecificity = threadSearchSpecificity(thread.search?.snippet, query);
  if (kind === "user") return {tier:1, specificity:snippetSpecificity};
  if (kind === "assistant") return {tier:2, specificity:snippetSpecificity};
  if (kind === "title") return {tier:0, specificity:snippetSpecificity};
  return null;
}

function mergedThreadSearchCandidates() {
  const candidates = new Map();
  for (const thread of state.threadSearch.results) {
    candidates.set(`${thread._projectId || ""}:${thread.id}`, thread);
  }
  for (const thread of state.threads) {
    const key = `${thread._projectId || ""}:${thread.id}`;
    const remote = candidates.get(key);
    candidates.set(key, remote ? {...remote, ...thread, search:remote.search} : thread);
  }
  return [...candidates.values()];
}

async function runThreadSearch(rawQuery, generation) {
  const projects = state.activeProject ? [state.activeProject] : [...state.projects];
  const results = new Array(projects.length).fill(null).map(() => []);
  const errors = [];
  let nextIndex = 0;
  const worker = async () => {
    while (generation === state.threadSearch.generation) {
      const index = nextIndex++;
      if (index >= projects.length) return;
      const project = projects[index];
      try {
        const query = new URLSearchParams({project_id:project.id, query:rawQuery, limit:String(THREAD_SEARCH_RESULT_LIMIT)});
        const data = await api(`/api/threads/search?${query}`);
        results[index] = Array.isArray(data.data) ? data.data : [];
      } catch (error) {
        errors.push(`${project.name}: ${error.message}`);
      }
    }
  };
  await Promise.all(Array.from({length:Math.min(3, Math.max(1, projects.length))}, worker));
  if (generation !== state.threadSearch.generation) return;
  state.threadSearch.results = results.flat();
  state.threadSearch.loading = false;
  state.threadSearch.error = errors.length === projects.length && projects.length ? errors[0] : "";
  renderThreads();
}

function handleThreadSearchInput() {
  clearTimeout(state.threadSearch.timer);
  const rawQuery = el("thread-search").value.replace(/\s+/g, " ").trim();
  const query = normalizeThreadSearchText(rawQuery);
  const generation = ++state.threadSearch.generation;
  state.threadSearch.query = query;
  state.threadSearch.results = [];
  state.threadSearch.error = "";
  state.threadSearch.loading = query.length >= 2;
  renderThreads();
  if (query.length < 2) return;
  state.threadSearch.timer = setTimeout(() => {
    void runThreadSearch(rawQuery, generation);
  }, THREAD_SEARCH_DEBOUNCE_MS);
}

function renderThreads() {
  const query = normalizeThreadSearchText(el("thread-search").value);
  selectors.threadList.innerHTML = "";
  const filtered = query
    ? mergedThreadSearchCandidates()
      .map(thread => ({thread, rank:threadSearchRank(thread, query)}))
      .filter(item => item.rank)
      .sort((left, right) => left.rank.tier - right.rank.tier
        || left.rank.specificity - right.rank.specificity
        || threadActivityTimestamp(right.thread) - threadActivityTimestamp(left.thread))
      .slice(0, THREAD_SEARCH_RESULT_LIMIT)
      .map(item => item.thread)
    : state.threads;
  el("conversation-home-title").textContent = state.activeProject ? `Conversas de ${state.activeProject.name}` : "Conversas recentes";
  el("conversation-home-description").textContent = query
    ? state.threadSearch.loading
      ? "Pesquisando títulos, solicitações e respostas do Codex…"
      : "Títulos primeiro, depois solicitações do usuário e, por último, respostas do Codex."
    : state.activeProject
      ? "Ordenadas pela conclusão ou pelo início mais recente da execução."
      : "Todos os projetos, ordenados pela conclusão ou pelo início mais recente da execução.";
  el("project-new-thread").classList.toggle("hidden", !state.activeProject);
  const empty = el("conversation-home-empty");
  empty.classList.toggle("hidden", filtered.length > 0 || state.threadSearch.loading);
  if (!filtered.length) {
    const heading = empty.querySelector("strong");
    const detail = empty.querySelector("span");
    if (heading) heading.textContent = state.threadSearch.error ? "Não foi possível concluir a pesquisa" : "Nenhuma conversa encontrada";
    if (detail) detail.textContent = state.threadSearch.error || (query
      ? "Tente outras palavras do título ou da solicitação."
      : "Selecione um projeto e inicie uma nova conversa.");
    return;
  }
  const visibleThreads = query ? filtered : state.activeProject ? filtered.slice(0, PROJECT_CONVERSATION_LIMIT) : filtered;
  visibleThreads.forEach(thread => {
    const button = document.createElement("button");
    button.className = `nav-item recent-thread-card ${state.activeThreadId === thread.id ? "active" : ""}`;
    const title = conversationTitle(thread);
    const cleanedPreview = visibleConversationText(thread.clc?.request_preview || thread.preview);
    const matchedPreview = query ? String(thread.search?.snippet || "").trim() : "";
    const preview = matchedPreview || (cleanedPreview && cleanedPreview !== title ? cleanedPreview : "Clique para abrir o contexto completo");
    const statusClass = threadStatus(thread);
    const pendingAction = pendingUserActionForThread(thread.id);
    const waitingLabel = pendingAction ? userActionLabel(pendingAction) : threadUserActionLabel(thread);
    const activity = threadActivityTimestamp(thread);
    const projectName = thread._projectName || state.activeProject?.name || "Projeto";
    const statusLabel = statusClass === "failed"
      ? '<span class="thread-state failed">Processo encerrado</span>'
      : statusClass === "waiting"
        ? `<span class="thread-state waiting">${escapeHTML(waitingLabel)}</span>`
        : statusClass === "active" ? '<span class="thread-state active">Em execução</span>' : "";
    button.innerHTML = `<span class="nav-dot ${statusClass}"></span><span class="nav-copy"><span class="recent-thread-meta"><span class="project-badge">${escapeHTML(projectName)}</span>${statusLabel}<time>${escapeHTML(formatTime(activity) || "")}</time></span><span class="nav-title">${escapeHTML(title)}</span><span class="nav-preview">${escapeHTML(preview || "")}</span></span><span class="recent-thread-arrow" aria-hidden="true">›</span>`;
    button.addEventListener("click", async () => {
      if (thread._projectId && state.activeProject?.id !== thread._projectId) await selectProject(thread._projectId);
      await openThread(thread.id);
    });
    selectors.threadList.appendChild(button);
  });
}

async function openThread(threadId) {
  const threadSummary = state.threads.find(thread => thread.id === threadId);
  const projectId = threadSummary?._projectId || state.activeProject?.id;
  const loadId = crypto.randomUUID();
  state.conversationViewGeneration += 1;
  state.conversationAutoFollow = true;
  state.conversationScrollInteractionUntil = 0;
  state.conversationScrollInteractionVersion += 1;
  state.threadLoadId = loadId;
  state.activeThreadId = threadId;
  state.messageRenderLimit = 100;
  state.activeTurnId = null;
  state.turnSubmissionPending = false;
  state.items.clear();
  state.diff = "";
  selectors.title.textContent = conversationTitle(threadSummary);
  el("rename-thread").disabled = false;
  el("archive-thread").disabled = false;
  renderThreads();
  renderMessages();
  renderApprovals();
  renderDiff();
  updateRunningUI();
  closeMobilePanels();
  state.items.set(`thread-loading-${loadId}`, {id:`thread-loading-${loadId}`, type:"reasoning", text:"Carregando o contexto da conversa…", status:"inProgress"});
  renderMessages();
  const cachedThread = state.threadDetails.get(threadId);
  if (cachedThread) hydrateThread(cachedThread);
  try {
    void loadToolProfile(threadId);
    const data = await api(`/api/threads/${encodeURIComponent(threadId)}?project_id=${encodeURIComponent(projectId)}`);
    if (state.threadLoadId !== loadId || state.activeThreadId !== threadId) return;
    state.threadDetails.set(threadId, data.thread || {});
    hydrateThread(data.thread || {});
    api(`/api/threads/${encodeURIComponent(threadId)}/resume?project_id=${encodeURIComponent(projectId)}`, {method:"POST"})
      .catch(error => toast(`A conversa foi aberta, mas não pôde ser retomada: ${error.message}`, "error"));
  } catch (error) { toast(error.message, "error"); }
}

function hydrateThread(thread) {
  state.items.clear();
  state.activeTurnId = null;
  state.turnSubmissionPending = false;
  state.activeTurnStartedAt = 0;
  state.executionActivityAt = 0;
  selectors.title.textContent = conversationTitle(thread);
  const turns = thread.turns || [];
  const threadId = thread.id || state.activeThreadId;
  let terminal = state.threadTerminalStatuses.get(threadId);
  const latestTurn = turns.at(-1);
  const latestStatus = String(latestTurn?.status || "").toLowerCase();
  if (!terminal && (["failed", "error"].includes(latestStatus) || latestTurn?.error)) {
    const rawError = latestTurn?.error;
    const detail = typeof rawError === "string" ? rawError : rawError?.message || "A execução terminou com falha.";
    const message = processEndedMessage(detail);
    recordThreadTerminalStatus(threadId, message);
    terminal = state.threadTerminalStatuses.get(threadId);
  }
  for (const turn of turns) {
    for (const item of turn.items || []) state.items.set(item.id || crypto.randomUUID(), item);
    if (!terminal && turn.status === "inProgress") {
      state.activeTurnId = turn.id;
      state.activeTurnStartedAt = Date.parse(turn.startedAt || turn.started_at || "") || Date.now();
      state.executionActivityAt = Date.now();
    }
  }
  if (terminal) {
    const errorId = `process-ended-${threadId}`;
    state.items.set(errorId, {id:errorId, type:"error", message:terminal.message});
  }
  renderMessages();
  updateRunningUI();
}

async function newThread() {
  if (!state.activeProject) {
    toast("Selecione um projeto para iniciar uma nova conversa.");
    selectors.sidebar.classList.add("open");
    return;
  }
  state.conversationViewGeneration += 1;
  state.conversationAutoFollow = true;
  state.conversationScrollInteractionUntil = 0;
  state.conversationScrollInteractionVersion += 1;
  state.threadLoadId = crypto.randomUUID();
  state.activeThreadId = null;
  state.activeTurnId = null;
  state.turnSubmissionPending = false;
  state.items.clear();
  state.diff = "";
  selectors.title.textContent = "Nova conversa";
  el("rename-thread").disabled = true;
  el("archive-thread").disabled = true;
  renderThreads();
  renderMessages();
  renderApprovals();
  renderDiff();
  closeMobilePanels();
  setConversationContextUI();
  updateRunningUI();
  await loadToolProfile(null);
  selectors.prompt.focus();
}

function extractContent(item) {
  if (typeof item.text === "string") return item.text;
  if (typeof item.message === "string") return item.message;
  if (typeof item.content === "string") return item.content;
  if (Array.isArray(item.content)) return item.content.map(part => part.text || part.output_text || part.input_text || "").filter(Boolean).join("\n");
  if (typeof item.summary === "string") return item.summary;
  if (Array.isArray(item.summary)) return item.summary.map(part => part.text || part).join("\n");
  return item._streamText || "";
}

function attachmentIdFromPath(value) {
  const match = String(value || "").match(/(?:^|\/)([a-f0-9]{32})(?:-|$)/i);
  return match?.[1] || "";
}

function messageImageSource(part) {
  const direct = part?.preview_url || part?.previewUrl || part?.image_url || part?.imageUrl || part?.url || "";
  if (/^\/api\/projects\//.test(direct) || /^data:image\//i.test(direct) || /^blob:/i.test(direct)) return direct;
  const attachmentId = attachmentIdFromPath(part?.path || part?.localPath || part?.filePath);
  if (!attachmentId || !state.activeProject?.id) return "";
  return `/api/projects/${encodeURIComponent(state.activeProject.id)}/attachments/${encodeURIComponent(attachmentId)}`;
}

function messageImageAttachments(item) {
  const candidates = [];
  for (const reference of item?.references || []) {
    if (String(reference?.type || "").toLowerCase() === "image") candidates.push(reference);
  }
  for (const part of Array.isArray(item?.content) ? item.content : []) {
    if (/image/i.test(String(part?.type || ""))) candidates.push(part);
  }
  const seen = new Set();
  return candidates.flatMap((part, index) => {
    const src = messageImageSource(part);
    if (!src || seen.has(src)) return [];
    seen.add(src);
    return [{src, name:String(part?.name || part?.filename || `Imagem ${index + 1}`)}];
  });
}

function messageAttachmentsHTML(item) {
  const images = messageImageAttachments(item);
  if (!images.length) return "";
  return `<div class="message-attachments" aria-label="Imagens anexadas">${images.map(image => `<img src="${escapeHTML(image.src)}" alt="${escapeHTML(image.name)}" loading="lazy">`).join("")}</div>`;
}

function commandText(command) {
  if (Array.isArray(command)) return command.join(" ");
  if (command && typeof command === "object") return JSON.stringify(command, null, 2);
  return String(command || "");
}

function activityStatus(status) {
  const labels = {inProgress:"Em andamento", completed:"Concluído", failed:"Falhou", declined:"Não autorizado"};
  return labels[status] || "Em andamento";
}

function commandActivitySummary(item) {
  const command = commandText(item.command).toLowerCase();
  if (/pytest|npm test|test:|smoke|lint|eslint|tsc|compile/.test(command)) return "Verificando se as alterações funcionam corretamente.";
  if (/build|package|tar |zip |docker build/.test(command)) return "Preparando uma nova versão do projeto.";
  if (/install|deploy|dpkg|systemctl|service /.test(command)) return "Aplicando e conferindo a atualização no servidor.";
  if (/curl|wget|health|status/.test(command)) return "Verificando se o serviço está disponível e saudável.";
  if (/rg |grep |find |get-content|select-object|ls |dir /.test(command)) return "Consultando os arquivos e as informações necessárias.";
  if (/git /.test(command)) return "Revisando e organizando as alterações do projeto.";
  return "Executando uma etapa técnica necessária para concluir a solicitação.";
}

function toolActivitySummary(item) {
  const name = `${item.server || ""} ${item.tool || ""}`.toLowerCase();
  if (/browser|web|chrome/.test(name)) return "Consultando ou operando uma página necessária para a tarefa.";
  if (/desktop|computer|screen/.test(name)) return "Operando a interface do computador para avançar a tarefa.";
  if (/read|search|list|status|inspect/.test(name)) return "Consultando as informações necessárias.";
  if (/write|edit|change|deploy|install|exec/.test(name)) return "Aplicando uma etapa necessária da alteração.";
  return "Usando uma ferramenta especializada para avançar a tarefa.";
}

function technicalDetails(content) {
  if (!content) return "";
  return `<details class="technical-details"><summary>Detalhes técnicos</summary>${content}</details>`;
}

function technicalActivityGroupCard(item) {
  const activities = item.items || [];
  const count = activities.length;
  const statusCounts = activities.reduce((counts, activity) => {
    const status = activity.status || "inProgress";
    counts[status] = (counts[status] || 0) + 1;
    return counts;
  }, {});
  const parts = [
    statusCounts.inProgress ? `${statusCounts.inProgress} em andamento` : "",
    statusCounts.failed ? `${statusCounts.failed} ${statusCounts.failed === 1 ? "falhou" : "falharam"}` : "",
    statusCounts.declined ? `${statusCounts.declined} não ${statusCounts.declined === 1 ? "autorizada" : "autorizadas"}` : "",
    statusCounts.completed ? `${statusCounts.completed} ${statusCounts.completed === 1 ? "concluída" : "concluídas"}` : "",
  ].filter(Boolean);
  const onlyCompleted = statusCounts.completed === count;
  const summary = onlyCompleted ? `${count} atividades concluídas` : `${count} atividades • ${parts.join(" • ")}`;
  const groupStatus = statusCounts.inProgress ? "inProgress" : (statusCounts.failed || statusCounts.declined) ? "failed" : "completed";
  return `<details class="technical-activity-group" data-group-status="${groupStatus}" data-item-id="${escapeHTML(item.id || "")}">
    <summary><span class="tool-status ${groupStatus}">${escapeHTML(summary)}</span><span class="technical-activity-group-hint">Ver atividades</span></summary>
    <div class="technical-activity-group-items">${activities.map(itemCard).join("")}</div>
  </details>`;
}

function nestedValues(value, seen = new Set()) {
  if (value == null || typeof value !== "object" || seen.has(value)) return [];
  seen.add(value);
  const values = [value];
  for (const child of Array.isArray(value) ? value : Object.values(value)) values.push(...nestedValues(child, seen));
  return values;
}

function browserSessionURL(items) {
  let sawBlank = false;
  for (const item of [...items].reverse()) {
    const direct = String(item?.arguments?.url || item?.url || "").trim();
    if (/^https?:\/\//i.test(direct)) return direct;
    if (direct === "about:blank") sawBlank = true;
    for (const value of nestedValues(item)) {
      for (const candidate of [value.url, value.pageUrl, value.href]) {
        if (/^https?:\/\//i.test(String(candidate || "").trim())) return String(candidate).trim();
        if (String(candidate || "").trim() === "about:blank") sawBlank = true;
      }
      for (const text of Object.values(value).filter(candidate => typeof candidate === "string")) {
        const match = text.match(/(?:^|\n)-?\s*Page URL:\s*(https?:\/\/[^\s]+)/i);
        if (match) return match[1];
        if (/(?:^|\n)-?\s*Page URL:\s*about:blank(?:\s|$)/i.test(text)) sawBlank = true;
      }
    }
  }
  return sawBlank ? "about:blank" : "";
}

function browserSessionPreview(items) {
  for (const item of items) {
    for (const value of nestedValues(item)) {
      const mime = String(value.mimeType || value.mime_type || "");
      if (value.type === "image" && value.data && /^image\//.test(mime)) return `data:${mime};base64,${value.data}`;
      const source = String(value.image_url || value.imageUrl || value.src || "");
      if (/^data:image\/(?:png|jpe?g|webp);base64,/i.test(source)) return source;
    }
  }
  return "";
}

function browserActivityGroupCard(item) {
  const activities = item.items || [];
  const url = browserSessionURL(activities);
  const unavailable = !url || url === "about:blank";
  const preview = browserSessionPreview(activities);
  const active = activities.some(activity => (activity.status || "inProgress") === "inProgress");
  const failed = activities.some(activity => activity.status === "failed");
  const status = active ? "inProgress" : (failed || unavailable) ? "failed" : "completed";
  const statusLabel = active ? "Navegando agora" : failed ? "A página precisa de atenção" : unavailable ? "Página não disponível" : "Página pronta";
  let host = "Nenhuma página aberta";
  try { if (!unavailable) host = new URL(url).hostname.replace(/^www\./, ""); } catch { /* URL label remains generic. */ }
  const latestTool = String(activities[0]?.tool || "");
  const detail = /screenshot/.test(latestTool) ? "Prévia atualizada" : /navigate/.test(latestTool) ? "Página aberta" : "Sessão do navegador";
  const previewQuery = new URLSearchParams({thread_id:String(state.activeThreadId || ""), card:String(item.id || "")});
  const livePreview = !preview && !unavailable && state.activeThreadId && item.id === state.browserPreviewGroupId;
  const visual = preview
    ? `<img class="browser-session-preview-image" src="${escapeHTML(preview)}" alt="Prévia da página ${escapeHTML(host)}" loading="lazy">`
    : livePreview
      ? `<div class="browser-session-preview-empty" data-browser-preview-empty aria-hidden="true"><span>◉</span><strong>${escapeHTML(host)}</strong><small>Carregando a última página aberta…</small></div><img class="browser-session-preview-image" src="/api/remote-desktop/browser-preview?${escapeHTML(previewQuery.toString())}" alt="Print da última página aberta em ${escapeHTML(host)}" data-browser-preview-image hidden>`
    : `<div class="browser-session-preview-empty" aria-hidden="true"><span>◉</span><strong>${escapeHTML(host)}</strong><small>A página ao vivo abre no visor seguro.</small></div>`;
  const details = activities.map(activity => `${activity.server || "MCP"}/${activity.tool || ""}`).join("\n");
  return `<article class="message-card tool browser-session-card" data-item-id="${escapeHTML(item.id || "")}" data-browser-status="${status}">
    <div class="browser-session-header">
      <span class="browser-session-icon" aria-hidden="true">◉</span>
      <div><strong>Navegador isolado da conversa</strong><small>${escapeHTML(detail)} • ${escapeHTML(host)}</small></div>
      <span class="browser-session-state ${status}">${escapeHTML(statusLabel)}</span>
    </div>
    <div class="browser-session-preview">${visual}</div>
    <div class="browser-session-actions">
      <button type="button" class="primary-button browser-session-open" data-browser-open="${escapeHTML(url)}"><span aria-hidden="true">◉</span><span>Ver e controlar ao vivo</span></button>
      <span class="browser-session-hint">Mostra a mesma janela usada pelo Codex; mouse, toque e teclado ficam disponíveis.</span>
      ${technicalDetails(`<pre class="tool-output">${escapeHTML(details)}</pre>`)}
    </div>
  </article>`;
}

function armBrowserSessionPreviews() {
  selectors.messageList.querySelectorAll("[data-browser-preview-image]").forEach(image => {
    const fallback = image.previousElementSibling;
    const reveal = () => {
      image.hidden = false;
      if (fallback) fallback.hidden = true;
    };
    const fail = () => {
      image.remove();
      const note = fallback?.querySelector("small");
      if (note) note.textContent = "A prévia não está mais disponível; abra o visor ao vivo para continuar.";
    };
    image.addEventListener("load", reveal, {once:true});
    image.addEventListener("error", fail, {once:true});
    if (image.complete) image.naturalWidth ? reveal() : fail();
  });
}

function androidActivityGroupCard(item) {
  const activities = item.items || [];
  const active = activities.some(activity => (activity.status || "inProgress") === "inProgress");
  const failed = activities.some(activity => activity.status === "failed");
  const status = active ? "inProgress" : failed ? "failed" : "completed";
  const statusLabel = active ? "Automatizando agora" : failed ? "O aplicativo precisa de atenção" : "Android pronto";
  const latestTool = String(activities[0]?.tool || commandText(activities[0]?.command) || "");
  const detail = /install|play/i.test(latestTool) ? "Aplicativo preparado" : /ui|source|snapshot/i.test(latestTool) ? "Interface estruturada lida" : "Sessão Android";
  const details = activities.map(activity => activity.type === "mcpToolCall"
    ? `${activity.server || "MCP"}/${activity.tool || ""}`
    : commandText(activity.command)).join("\n");
  return `<article class="message-card tool browser-session-card android-session-card" data-item-id="${escapeHTML(item.id || "")}" data-browser-status="${status}">
    <div class="browser-session-header">
      <span class="browser-session-icon" aria-hidden="true">▣</span>
      <div><strong>Android da conversa ao vivo</strong><small>${escapeHTML(detail)} • Waydroid Android 13</small></div>
      <span class="browser-session-state ${status}">${escapeHTML(statusLabel)}</span>
    </div>
    <div class="browser-session-preview">
      <div class="browser-session-preview-empty" aria-hidden="true"><span>▣</span><strong>Aplicativo Android</strong><small>A mesma tela usada pelo Codex abre no visor seguro.</small></div>
    </div>
    <div class="browser-session-actions">
      <button type="button" class="primary-button browser-session-open" data-android-open><span aria-hidden="true">▣</span><span>Ver e controlar ao vivo</span></button>
      <span class="browser-session-hint">Mostra a mesma sessão Android usada pelo Codex; mouse, toque e teclado ficam disponíveis.</span>
      ${technicalDetails(`<pre class="tool-output">${escapeHTML(details)}</pre>`)}
    </div>
  </article>`;
}

function itemCard(item) {
  const type = item.type || "unknown";
  const id = item.id || "";
  if (type === "technicalActivityGroup") return technicalActivityGroupCard(item);
  if (type === "browserActivityGroup") return browserActivityGroupCard(item);
  if (type === "androidActivityGroup") return androidActivityGroupCard(item);
  let cls = "tool";
  let label = type;
  let body = "";
  if (["userMessage", "user_message"].includes(type)) {
    const text = visibleConversationText(extractContent(item));
    cls = "user"; label = "Você"; body = `<div class="message-text">${escapeHTML(text)}</div>${messageAttachmentsHTML(item)}`;
  } else if (["agentMessage", "assistantMessage", "agent_message"].includes(type)) {
    cls = "assistant"; label = "Codex"; body = escapeHTML(extractContent(item));
  } else if (type === "reasoning") {
    const reasoning = extractContent(item);
    cls = "reasoning";
    label = "Contexto";
    body = `<details class="reasoning-details"><summary>${escapeHTML(compactReasoningPreview(reasoning))}</summary><div>${escapeHTML(reasoning)}</div></details>`;
  } else if (type === "plan") {
    cls = "assistant"; label = "Plano"; body = escapeHTML(extractContent(item));
  } else if (type === "commandExecution") {
    label = "Atividade";
    const status = item.status || "inProgress";
    const output = item.aggregatedOutput || item._output || "";
    const details = `<pre class="tool-command">${escapeHTML(commandText(item.command))}</pre>${output ? `<pre class="tool-output">${escapeHTML(output)}</pre>` : ""}`;
    body = `<span class="tool-status ${escapeHTML(status)}">${escapeHTML(activityStatus(status))}</span><p>${escapeHTML(commandActivitySummary(item))}</p>${technicalDetails(details)}`;
  } else if (type === "fileChange") {
    label = "Atividade";
    const status = item.status || "inProgress";
    const changes = (item.changes || []).map(change => `${change.path || change.filePath || "arquivo"}: ${change.kind || change.type || "alterado"}`).join("\n");
    body = `<span class="tool-status ${escapeHTML(status)}">${escapeHTML(activityStatus(status))}</span><p>Atualizando os arquivos necessários.</p>${changes ? technicalDetails(`<pre class="tool-output">${escapeHTML(changes)}</pre>`) : ""}`;
  } else if (type === "mcpToolCall") {
    label = "Atividade";
    const status = item.status || "inProgress";
    const details = `<pre class="tool-output">${escapeHTML(`${item.server || "MCP"}/${item.tool || ""}\n${JSON.stringify(item.arguments || {}, null, 2)}`)}</pre>`;
    body = `<span class="tool-status ${escapeHTML(status)}">${escapeHTML(activityStatus(status))}</span><p>${escapeHTML(toolActivitySummary(item))}</p>${technicalDetails(details)}`;
  } else if (type === "webSearch") {
    label = "Pesquisa web"; body = escapeHTML(item.query || JSON.stringify(item.action || {}));
  } else if (type === "imageView") {
    label = "Imagem analisada"; body = escapeHTML(item.path || "");
  } else if (type === "error") {
    cls = "error"; label = "Erro"; body = escapeHTML(item.message || extractContent(item));
  } else {
    const text = extractContent(item);
    body = text ? escapeHTML(text) : `<pre class="tool-output">${escapeHTML(JSON.stringify(item, null, 2))}</pre>`;
  }
  const handledFailureClass = item._failureHandled ? " handled-failure" : "";
  return `<article class="message-card ${cls}${handledFailureClass}" data-item-id="${escapeHTML(id)}"><div class="message-meta"><span>${escapeHTML(label)}</span>${item.status ? `<span>${escapeHTML(item.status)}</span>` : ""}</div><div class="message-body">${body}</div></article>`;
}

function executionStatusSummary() {
  const waiting = executionWaitingState();
  if (waiting === "input") return `${activeUserActionLabel()} para a conversa continuar.`;
  if (waiting === "approval") return `${activeUserActionLabel()} ou recuse a solicitação para a conversa continuar.`;
  const recent = [...state.items.values()].reverse();
  const activeTechnical = recent.find(item =>
    item.status === "inProgress" && ["commandExecution", "fileChange", "mcpToolCall"].includes(item.type));
  if (activeTechnical?.type === "commandExecution") return commandActivitySummary(activeTechnical);
  if (activeTechnical?.type === "fileChange") return "Atualizando os arquivos necessários.";
  if (activeTechnical?.type === "mcpToolCall") return toolActivitySummary(activeTechnical);
  const reasoning = recent.find(item => item.type === "reasoning" && extractContent(item).trim());
  if (reasoning) {
    const summary = compactReasoningPreview(extractContent(reasoning));
    if (summary !== "Preparando a próxima etapa…") return summary;
  }
  const lastTechnical = recent.find(item => ["commandExecution", "fileChange", "mcpToolCall"].includes(item.type));
  if (lastTechnical?.type === "commandExecution") return `Última etapa: ${commandActivitySummary(lastTechnical)} Preparando a próxima.`;
  if (lastTechnical?.type === "fileChange") return "Última etapa: arquivos atualizados. Preparando a próxima.";
  if (lastTechnical?.type === "mcpToolCall") return `Última etapa: ${toolActivitySummary(lastTechnical)} Preparando a próxima.`;
  return "Analisando a solicitação e preparando a próxima etapa.";
}

function approvalThreadId(approval) {
  return String(approval?.params?.threadId || "");
}

function approvalBelongsToActiveThread(approval) {
  const threadId = approvalThreadId(approval);
  return Boolean(threadId && state.activeThreadId && threadId === state.activeThreadId);
}

function pendingUserActionForThread(threadId) {
  if (!threadId) return null;
  return [...state.approvals.values()].find(approval =>
    !approval.resolved
    && approvalThreadId(approval) === threadId
    && requestWaitingKind(approval.method, approval.params));
}

function findApprovalThread(approval) {
  const threadId = approvalThreadId(approval);
  if (!threadId) return null;
  return state.threads.find(thread => thread.id === threadId)
    || [...state.projectThreads.values()].flat().find(thread => thread.id === threadId)
    || (state.threadDetails.get(threadId)?.id ? state.threadDetails.get(threadId) : null);
}

function pendingUserAction() {
  return pendingUserActionForThread(state.activeThreadId);
}

function activeUserActionLabel() {
  const pending = pendingUserAction();
  return pending ? userActionLabel(pending) : "Responder ao Codex";
}

function syncThreadWaitingStatus(threadId) {
  if (!threadId) return;
  const pending = [...state.approvals.values()].find(approval =>
    !approval.resolved
    && approval.params?.threadId === threadId
    && requestWaitingKind(approval.method, approval.params));
  const waiting = pending ? requestWaitingKind(pending.method, pending.params) : "";
  updateThreadRuntimeStatus(threadId, {type:"active", activeFlags:waiting ? [waiting === "input" ? "waitingOnUserInput" : "waitingOnApproval"] : []});
}

function activeThreadSummary() {
  if (!state.activeThreadId) return null;
  return state.threads.find(thread => thread.id === state.activeThreadId)
    || [...state.projectThreads.values()].flat().find(thread => thread.id === state.activeThreadId)
    || null;
}

function executionWaitingState() {
  const request = pendingUserAction();
  return request ? requestWaitingKind(request.method, request.params) : "";
}

function compactElapsed(milliseconds) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes}min ${String(remainder).padStart(2, "0")}s`;
}

function executionIsActive() {
  return state.turnSubmissionPending || Boolean(state.activeThreadId && state.activeTurnId);
}

function executionStatusHTML() {
  if (!executionIsActive()) return "";
  const waiting = executionWaitingState();
  const now = Date.now();
  const startedAt = state.activeTurnStartedAt || now;
  const activityAt = state.executionActivityAt || startedAt;
  const quietSeconds = Math.max(0, Math.floor((now - activityAt) / 1000));
  const connection = waiting ? "Execução pausada até sua ação; recuperação automática ativa"
    : state.socket?.readyState === WebSocket.OPEN
      ? (quietSeconds < 15 ? "Recebendo atualizações" : `Aguardando a próxima atualização há ${compactElapsed(now - activityAt)}`)
      : "Reconectando ao acompanhamento; a execução pode continuar no servidor";
  const title = waiting ? activeUserActionLabel() : "Codex está trabalhando";
  return `<section class="execution-status-card ${waiting ? "waiting" : ""}" role="button" tabindex="0" aria-live="polite" aria-label="${escapeHTML(title)}. Clique para voltar a acompanhar o topo." title="Voltar a acompanhar o topo">
    <span class="execution-status-pulse" aria-hidden="true"></span>
    <div class="execution-status-copy">
      <strong>${escapeHTML(title)}</strong>
      <p>${escapeHTML(executionStatusSummary())}</p>
      <small><span data-execution-elapsed>Em execução há ${escapeHTML(compactElapsed(now - startedAt))}</span> • <span data-execution-connection>${escapeHTML(connection)}</span></small>
    </div>
  </section>`;
}

function deferredUserActionHTML() {
  if (executionIsActive() || pendingUserAction()) return "";
  const thread = activeThreadSummary() || state.threadDetails.get(state.activeThreadId);
  if (!thread?.clc?.awaiting_user_action) return "";
  const label = threadUserActionLabel(thread);
  return `<section class="execution-status-card waiting user-action-pending-card" role="status" aria-live="polite">
    <span class="execution-status-pulse" aria-hidden="true"></span>
    <div class="execution-status-copy">
      <strong>${escapeHTML(label)}</strong>
      <p>Esta conversa depende da sua resposta para continuar.</p>
      <small>Pendência mantida até você enviar a próxima mensagem.</small>
    </div>
  </section>`;
}

function refreshExecutionStatusClock() {
  if (!executionIsActive()) return;
  const waiting = executionWaitingState();
  const now = Date.now();
  const startedAt = state.activeTurnStartedAt || now;
  const activityAt = state.executionActivityAt || startedAt;
  const quietSeconds = Math.max(0, Math.floor((now - activityAt) / 1000));
  document.querySelectorAll("[data-execution-elapsed]").forEach(node => {
    node.textContent = `Em execução há ${compactElapsed(now - startedAt)}`;
  });
  document.querySelectorAll("[data-execution-connection]").forEach(node => {
    node.textContent = waiting ? "Execução pausada até sua ação; recuperação automática ativa" : state.socket?.readyState === WebSocket.OPEN
      ? (quietSeconds < 15 ? "Recebendo atualizações" : `Aguardando a próxima atualização há ${compactElapsed(now - activityAt)}`)
      : "Reconectando ao acompanhamento; a execução pode continuar no servidor";
  });
}

function syncExecutionTicker() {
  if (executionIsActive()) {
    if (!state.activeTurnStartedAt) state.activeTurnStartedAt = Date.now();
    if (!state.executionActivityAt) state.executionActivityAt = state.activeTurnStartedAt;
    if (!state.executionTicker) state.executionTicker = setInterval(refreshExecutionStatusClock, 1000);
    return;
  }
  if (state.executionTicker) clearInterval(state.executionTicker);
  state.executionTicker = null;
  state.activeTurnStartedAt = 0;
  state.executionActivityAt = 0;
}

function setConversationScrollTop(value) {
  const chat = selectors.chat;
  if (!chat || chat.scrollTop === value) return;
  const previousBehavior = chat.style.scrollBehavior;
  chat.style.scrollBehavior = "auto";
  chat.scrollTop = value;
  chat.style.scrollBehavior = previousBehavior;
}

function syncConversationScrollAnchoring() {
  const chat = selectors.chat;
  if (!chat) return;
  // Full list replacements use the custom anchor below. While the operator is
  // reading older content, native anchoring also protects against late image,
  // font and disclosure resizing after the replacement has finished.
  chat.style.overflowAnchor = state.conversationAutoFollow ? "none" : "auto";
}

function pauseConversationAutoFollow() {
  const wasFollowing = state.conversationAutoFollow;
  state.conversationAutoFollow = false;
  state.conversationScrollInteractionUntil = Date.now() + 450;
  if (wasFollowing) state.conversationScrollInteractionVersion += 1;
  syncConversationScrollAnchoring();
}

function resumeConversationAutoFollow() {
  state.conversationAutoFollow = true;
  state.conversationScrollInteractionUntil = 0;
  state.conversationScrollInteractionVersion += 1;
  if (state.conversationRenderTimer) clearTimeout(state.conversationRenderTimer);
  state.conversationRenderTimer = null;
  if (state.conversationScrollRestoreFrame) cancelAnimationFrame(state.conversationScrollRestoreFrame);
  state.conversationScrollRestoreFrame = null;
  syncConversationScrollAnchoring();
  renderMessages();
}

function installConversationScrollControl() {
  const chat = selectors.chat;
  if (!chat) return;
  const pause = () => pauseConversationAutoFollow();
  chat.addEventListener("wheel", pause, {passive:true});
  chat.addEventListener("touchmove", pause, {passive:true});
  chat.addEventListener("pointerdown", event => {
    const bounds = chat.getBoundingClientRect();
    if (event.clientX >= bounds.right - 22) pause();
  }, {passive:true});
  chat.addEventListener("scroll", () => {
    // Catch keyboard, accessibility, scrollbar and browser-driven scrolling in
    // addition to wheel/touch gestures. Moving away from the newest edge is an
    // explicit reading position and must never be pulled back upward.
    if (state.conversationAutoFollow && chat.scrollTop > 2) {
      pauseConversationAutoFollow();
      return;
    }
    if (!state.conversationAutoFollow) state.conversationScrollInteractionUntil = Date.now() + 180;
  }, {passive:true});
  syncConversationScrollAnchoring();
}

function captureMessageScrollState() {
  const chat = selectors.chat;
  if (!chat) return null;
  const viewportTop = chat.getBoundingClientRect().top;
  // Sticky activity and approval cards are transient controls, not reading
  // anchors. Keep several real messages so regrouping one still leaves a
  // stable fallback.
  const anchors = [...chat.querySelectorAll("[data-item-id]")]
    .filter(node => node.getBoundingClientRect().bottom > viewportTop + 1)
    .slice(0, 6)
    .map(node => ({id:node.dataset.itemId || "", offset:node.getBoundingClientRect().top - viewportTop}))
    .filter(anchor => anchor.id);
  return {
    followNewest: state.conversationAutoFollow,
    interactionVersion: state.conversationScrollInteractionVersion,
    scrollTop: chat.scrollTop,
    anchors,
  };
}

function restoreMessageScrollState(scrollState) {
  const chat = selectors.chat;
  if (!chat || !scrollState) return;
  if (scrollState.interactionVersion !== state.conversationScrollInteractionVersion) return;
  if (scrollState.followNewest && state.conversationAutoFollow) {
    setConversationScrollTop(0);
    return;
  }
  if (state.conversationAutoFollow || Date.now() < state.conversationScrollInteractionUntil) return;
  setConversationScrollTop(scrollState.scrollTop);
  const availableItems = [...chat.querySelectorAll("[data-item-id]")];
  const savedAnchor = (scrollState.anchors || []).find(candidate =>
    availableItems.some(node => node.dataset.itemId === candidate.id));
  if (!savedAnchor) return;
  const anchor = availableItems.find(node => node.dataset.itemId === savedAnchor.id);
  const viewportTop = chat.getBoundingClientRect().top;
  setConversationScrollTop(chat.scrollTop + anchor.getBoundingClientRect().top - viewportTop - savedAnchor.offset);
}

function deferConversationRenderUntilScrollStops() {
  if (state.conversationRenderTimer) return;
  const renderWhenIdle = () => {
    const remaining = state.conversationScrollInteractionUntil - Date.now();
    if (!state.conversationAutoFollow && remaining > 0) {
      state.conversationRenderTimer = setTimeout(renderWhenIdle, remaining + 20);
      return;
    }
    state.conversationRenderTimer = null;
    renderMessages();
  };
  const delay = Math.max(20, state.conversationScrollInteractionUntil - Date.now() + 20);
  state.conversationRenderTimer = setTimeout(renderWhenIdle, delay);
}

function renderMessages() {
  syncConversationScrollAnchoring();
  if (!state.conversationAutoFollow && Date.now() < state.conversationScrollInteractionUntil) {
    deferConversationRenderUntilScrollStops();
    return;
  }
  if (state.conversationRenderTimer) clearTimeout(state.conversationRenderTimer);
  state.conversationRenderTimer = null;
  const scrollState = captureMessageScrollState();
  const items = groupTechnicalActivities(groupAndroidActivities(groupBrowserActivities(conversationItemsForDisplay([...state.items.values()].reverse()))));
  state.browserPreviewGroupId = items.find(item => item?.type === "browserActivityGroup")?.id || "";
  const visibleItems = items.slice(0, state.messageRenderLimit);
  selectors.empty.classList.toggle("hidden", items.length > 0);
  const approvals = [...state.approvals.values()]
    .filter(approval => !approval.resolved && approvalBelongsToActiveThread(approval))
    .reverse()
    .map(approval => approvalCardHTML(approval, true));
  const older = items.length > visibleItems.length
    ? `<button type="button" class="secondary-button load-older-messages" data-load-older-messages>Mostrar mais ${Math.min(100, items.length - visibleItems.length)} itens anteriores</button>`
    : "";
  const attentionCards = [executionStatusHTML(), deferredUserActionHTML(), ...approvals].filter(Boolean);
  const attentionStack = attentionCards.length
    ? `<div class="conversation-attention-stack" aria-label="Atividade e aprovações da conversa">${attentionCards.join("")}</div>`
    : "";
  selectors.messageList.innerHTML = [attentionStack, ...visibleItems.map(itemCard), older].filter(Boolean).join("");
  armBrowserSessionPreviews();
  // Restore before the browser paints the replaced list. Waiting only for the
  // next frame exposes one displaced frame and makes the top appear to jump
  // while streaming updates arrive. Keep just one follow-up correction for
  // layout work finalized by the browser in that frame.
  restoreMessageScrollState(scrollState);
  if (state.conversationScrollRestoreFrame) cancelAnimationFrame(state.conversationScrollRestoreFrame);
  state.conversationScrollRestoreFrame = requestAnimationFrame(() => {
    state.conversationScrollRestoreFrame = null;
    restoreMessageScrollState(scrollState);
  });
}

function setConversationContextUI() {
  const hasProject = Boolean(state.activeProject);
  selectors.prompt.disabled = !hasProject;
  selectors.send.disabled = !hasProject;
  selectors.prompt.placeholder = hasProject ? "Converse com o Codex…" : "Selecione um projeto para iniciar uma conversa";
}

function addLocalUserMessage(text, references = []) {
  const id = `local-${crypto.randomUUID()}`;
  state.items.set(id, {id, type:"userMessage", content:[{type:"text", text}], references:references.map(reference => ({...reference}))});
  renderMessages();
}

function showLocalActivity(message = "Preparando a execução…") {
  clearLocalActivity();
  const id = `local-activity-${crypto.randomUUID()}`;
  state.pendingActivityId = id;
  state.items.set(id, {id, type:"reasoning", text:message, status:"inProgress", localActivity:true});
  renderMessages();
}

function clearLocalActivity() {
  if (!state.pendingActivityId) return;
  state.items.delete(state.pendingActivityId);
  state.pendingActivityId = null;
}

async function sendMessage() {
  const message = selectors.prompt.value.trim();
  if (!message) return;
  if (!state.activeProject) return toast("Selecione ou adicione um projeto.", "error");
  if (!activeBridge().initialized) return toast(`${workspaceLabel()} não está disponível.`, "error");
  if (state.activeThreadId && (state.activeTurnId || state.turnSubmissionPending)) {
    if (typeof addMessageToQueue === "function") return addMessageToQueue();
    return toast("A execução atual ainda está em andamento. Aguarde a conclusão para enviar a próxima mensagem.", "error");
  }
  const viewGeneration = state.conversationViewGeneration;
  const initialThreadId = state.activeThreadId;
  clearThreadTerminalStatus(initialThreadId);
  setThreadAwaitingUserAction(initialThreadId, false);
  const messageReferences = (state.references || []).map(reference => ({...reference}));
  selectors.prompt.value = "";
  autoResizePrompt();
  addLocalUserMessage(message, messageReferences);
  state.turnSubmissionPending = true;
  state.activeTurnStartedAt = Date.now();
  state.executionActivityAt = state.activeTurnStartedAt;
  showLocalActivity();
  syncExecutionTicker();
  setStatus("busy", "Executando");
  selectors.send.disabled = true;
  const payload = {
    project_id: state.activeProject.id,
    message,
    model: selectors.model.value || null,
    effort: selectors.effort.value || null,
    service_tier: selectors.speed.value === "__default__" ? null : selectors.speed.value || null,
    network_access: selectors.network.value === "enabled",
    tools: state.toolProfile,
    references: messageReferences,
    collaboration_mode: state.composerMode === "plan" ? "plan" : null,
    goal_mode: state.composerMode === "goal",
  };
  try {
    let data;
    if (!state.activeThreadId) {
      data = await api("/api/threads", {method:"POST", body:JSON.stringify(payload)});
      if (state.conversationViewGeneration !== viewGeneration || state.activeThreadId !== initialThreadId) {
        state.turnSubmissionPending = false;
        syncExecutionTicker();
        await loadThreads();
        return;
      }
      state.activeThreadId = data.thread?.id;
      selectors.title.textContent = conversationTitle({
        ...(data.thread || {}),
        name:data.thread?.name || message,
        _projectName:state.activeProject?.name,
        _projectKind:state.activeProject?.kind,
      });
      el("rename-thread").disabled = false;
      el("archive-thread").disabled = false;
      if (data.toolProfile) state.toolProfile = normalizeToolProfile(data.toolProfile);
    } else {
      data = await api(`/api/threads/${encodeURIComponent(state.activeThreadId)}/messages`, {method:"POST", body:JSON.stringify(payload)});
      if (state.conversationViewGeneration !== viewGeneration || state.activeThreadId !== initialThreadId) {
        state.turnSubmissionPending = false;
        syncExecutionTicker();
        await loadThreads();
        return;
      }
    }
    state.turnSubmissionPending = false;
    if (data.queued) {
      state.activeTurnId = data.turn?.id || state.activeTurnId;
      updateRunningUI();
      if (typeof loadOperationQueue === "function") await loadOperationQueue();
      toast("Mensagem preservada na fila para depois da execução atual.", "success");
      return;
    }
    if (data.toolProfile) state.toolProfile = normalizeToolProfile(data.toolProfile);
    renderToolProfileChip();
    state.activeTurnId = data.turn?.id || null;
    state.references = [];
    state.composerMode = null;
    if (typeof renderOperationReferences === "function") renderOperationReferences();
    if (typeof window.renderComposerMode === "function") window.renderComposerMode();
    updateRunningUI();
    await loadThreads();
  } catch (error) {
    if (state.conversationViewGeneration !== viewGeneration) {
      await loadThreads().catch(() => null);
      return;
    }
    clearLocalActivity();
    state.turnSubmissionPending = false;
    state.items.set(`error-${crypto.randomUUID()}`, {id:crypto.randomUUID(), type:"error", message:error.message});
    state.activeTurnId = null;
    setStatus("error", "Falha");
    syncExecutionTicker();
    renderMessages();
  } finally {
    if (state.conversationViewGeneration === viewGeneration) selectors.send.disabled = false;
  }
}

async function interruptTurn() {
  if (!state.activeThreadId || !state.activeTurnId) return;
  try {
    await api(`/api/threads/${encodeURIComponent(state.activeThreadId)}/interrupt`, {method:"POST", body:JSON.stringify({turn_id:state.activeTurnId})});
    toast("Interrupção solicitada.");
  } catch (error) { toast(error.message, "error"); }
}

function updateRunningUI() {
  const running = Boolean(state.activeThreadId && state.activeTurnId);
  selectors.interrupt.classList.toggle("hidden", !running);
  selectors.send.classList.toggle("hidden", running);
  if (running) setStatus("busy", executionWaitingState() ? activeUserActionLabel() : "Executando");
  else if (activeBridge().initialized) syncActiveBridgeUI();
  syncExecutionTicker();
  renderMessages();
}

// ---------------------------------------------------------------------------
// App-server events, approvals and diffs
// ---------------------------------------------------------------------------

function connectEvents() {
  if (state.socketTimer) {
    clearTimeout(state.socketTimer);
    state.socketTimer = null;
  }
  if (state.socket) {
    const previous = state.socket;
    state.socket = null;
    previous.close(1000, "substituído");
  }
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/api/events`);
  state.socket = socket;
  socket.addEventListener("open", () => {
    state.socketRetry = 0;
    selectors.connectionLabel.textContent = state.identity === "localhost" ? "Acesso local" : state.identity;
  });
  socket.addEventListener("message", event => {
    try { handleEvent(JSON.parse(event.data)); } catch (error) { console.error(error); }
  });
  socket.addEventListener("close", () => {
    if (state.socket !== socket) return;
    state.socket = null;
    selectors.connectionLabel.textContent = "Reconectando…";
    const delay = Math.min(30000, 1000 * (2 ** state.socketRetry++));
    state.socketTimer = setTimeout(async () => {
      state.socketTimer = null;
      try {
        // The browser session may have expired while Android or a desktop PWA
        // was suspended. Renew it before another WebSocket handshake so an
        // unauthorized client does not retry forever and flood the service.
        await renewSession();
        connectEvents();
      } catch (error) {
        selectors.connectionLabel.textContent = "Autorização necessária";
        setDeviceAuthBusy(false);
        setDeviceAuthOverlay(true, error.message || "Autorize novamente este dispositivo.");
      }
    }, delay);
  });
}

async function notifyTurnCompletionFallback(workspace, params = {}) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (document.visibilityState === "visible" && document.hasFocus()) return;
  const registration = await navigator.serviceWorker?.ready;
  if (!registration) return;
  const subscription = await registration.pushManager?.getSubscription();
  if (subscription) return;
  const turn = params.turn || {};
  const threadId = String(params.threadId || turn.threadId || "");
  const turnId = String(turn.id || params.turnId || threadId || Date.now());
  const projectId = String(workspace || "system").startsWith("project:")
    ? String(workspace).split(":", 2)[1]
    : (state.threads.find(item => item.id === threadId)?._projectId || "system-control");
  const status = String(turn.status || "completed").toLowerCase();
  const title = status === "failed" ? "Atividade do Dex falhou" : status.includes("cancel")
    ? "Atividade do Dex interrompida" : "Atividade concluída no Dex";
  await registration.showNotification(title, {
    body:`${workspaceLabel(workspace)} • Abra a conversa para ver o resultado.`,
    icon:"/icons/codex-remoto-192.png",
    badge:"/icons/codex-remoto-192.png",
    tag:`dex-turn-${turnId}`,
    data:{url:`/?project=${encodeURIComponent(projectId)}&thread=${encodeURIComponent(threadId)}`},
  });
}

function handleEvent(event) {
  const rawWorkspace = event.workspace || "system";
  const workspace = workspaceGroup(rawWorkspace);
  const active = rawWorkspace === activeEventWorkspace() || (rawWorkspace === "projects" && workspace === activeWorkspace());
  if (event.kind === "bridge_status") {
    const bridgeWasInitialized = Boolean(state.bridges[workspace]?.initialized);
    state.bridges[workspace] = {
      ...(state.bridges[workspace] || {}),
      initialized:event.status === "ready" || (state.bridges[workspace]?.initialized && event.status === "idle"),
      last_error:event.error || "",
      status:event.status,
    };
    if (["stopped", "error"].includes(event.status) && bridgeWasInitialized) {
      markBridgeExecutionsFailed(rawWorkspace, event.error || (event.returncode !== undefined ? `código ${event.returncode}` : ""));
    }
    if (active) syncActiveBridgeUI();
    else addActivity(`${workspaceLabel(rawWorkspace)} • estado`, event.status === "ready" ? "Pronto" : (event.error || event.status));
    if (state.setup.active) refreshSetupState().catch(console.error);
    return;
  }
  if (event.kind === "server_request") {
    const request = event.request;
    const approval = {id:request.id, method:request.method, params:request.params || {}, workspace:rawWorkspace, resolved:false, receivedAt:Date.now()};
    const approvalKey = `${rawWorkspace}:${request.id}`;
    state.approvals.set(approvalKey, approval);
    const waitingKind = requestWaitingKind(request.method, request.params || {});
    const requestThreadId = String(request.params?.threadId || "");
    if (waitingKind && requestThreadId) {
      updateThreadRuntimeStatus(requestThreadId, {type:"active", activeFlags:[waitingKind === "input" ? "waitingOnUserInput" : "waitingOnApproval"]});
    }
    renderApprovals();
    const automaticAction = automaticConversationApprovalAction(approval);
    if (automaticAction) {
      queueMicrotask(() => respondApproval(approvalKey, automaticAction, {automatic:true}));
      return;
    }
    if (requestThreadId && requestThreadId === state.activeThreadId) {
      renderMessages();
      setStatus("busy", waitingKind ? userActionLabel(approval) : "Executando");
    }
    if (window.Notification?.permission === "granted" && waitingKind) new Notification(`${workspaceLabel(rawWorkspace)} • ${userActionLabel(approval)}`, {body:approvalTitle(request.method, request.params || {})});
    return;
  }
  if (event.kind !== "notification") return;
  const notification = event.notification;
  const method = notification.method;
  const params = notification.params || {};
  const eventThreadId = String(params.threadId || params.turn?.threadId || "");
  if (!eventThreadId || eventThreadId === state.activeThreadId) state.executionActivityAt = Date.now();
  let runtimeStatus = null;
  if (method === "thread/status/changed") runtimeStatus = params.status || null;
  else if (method === "turn/started") {
    clearThreadTerminalStatus(eventThreadId);
    setThreadAwaitingUserAction(eventThreadId, false);
    runtimeStatus = {type:"active", activeFlags:[]};
  } else if (method === "turn/completed") {
    const turnStatus = String(params.turn?.status || "completed").toLowerCase();
    const failed = ["failed", "error"].includes(turnStatus) || Boolean(params.turn?.error);
    if (failed) {
      const rawError = params.turn?.error;
      const detail = typeof rawError === "string" ? rawError : rawError?.message || "A execução terminou com falha.";
      recordThreadTerminalStatus(eventThreadId, processEndedMessage(detail));
      runtimeStatus = null;
    } else {
      clearThreadTerminalStatus(eventThreadId);
      const userActionLabel = completedTurnUserAction(params.turn || {});
      setThreadAwaitingUserAction(eventThreadId, Boolean(userActionLabel), userActionLabel || "");
      runtimeStatus = {type:"idle"};
    }
  }
  if (runtimeStatus && eventThreadId) {
    const updated = updateThreadRuntimeStatus(eventThreadId, runtimeStatus);
    if (!updated && !state.activeProject) loadThreads().catch(console.error);
  }
  if (method === "thread/status/changed" && eventThreadId === state.activeThreadId && runtimeStatus?.type === "idle") {
    state.activeTurnId = null;
    updateRunningUI();
  }
  if (method === "turn/completed") notifyTurnCompletionFallback(rawWorkspace, params).catch(console.error);
  if (["turn/completed", "thread/status/changed"].includes(method)) {
    window.setTimeout(() => void maybeStartAutomaticSystemUpdate(), 1500);
  }
  if (method === "serverRequest/resolved") {
    const approval = state.approvals.get(`${rawWorkspace}:${params.requestId}`);
    const belongsToActiveThread = approvalBelongsToActiveThread(approval);
    if (approval) {
      approval.resolved = true;
      syncThreadWaitingStatus(approvalThreadId(approval));
    }
    renderApprovals();
    if (belongsToActiveThread) updateRunningUI();
    return;
  }
  if (method === "clc/approvalExpired") {
    const approval = state.approvals.get(`${rawWorkspace}:${params.requestId}`);
    const expiredThreadId = String(params.threadId || approvalThreadId(approval) || eventThreadId);
    const belongsToActiveThread = Boolean(expiredThreadId && expiredThreadId === state.activeThreadId);
    if (approval) approval.resolved = true;
    syncThreadWaitingStatus(expiredThreadId);
    if (belongsToActiveThread) {
      const timeoutId = `approval-timeout-${params.requestId || crypto.randomUUID()}`;
      state.items.set(timeoutId, {
        id:timeoutId,
        type:"error",
        message:"A solicitação de aprovação ficou sem resposta e foi cancelada automaticamente para liberar a conversa. O Codex continuará ou explicará o bloqueio.",
      });
    }
    renderApprovals();
    if (belongsToActiveThread) updateRunningUI();
    addActivity("Recuperação automática", `Solicitação expirada após ${compactElapsed(Number(params.waitedSeconds || 0) * 1000)}.`, "error");
    return;
  }
  if (!active) {
    addActivity(`${workspaceLabel(rawWorkspace)} • ${method}`, summarizeEvent(params));
    if (["account/updated", "account/login/completed"].includes(method)) {
      loadAccount({}, workspaceGroup(rawWorkspace));
      if (method === "account/login/completed" && params.success && state.loginWorkspace === workspace) {
        el("login-status").textContent = `Autenticação concluída em ${workspaceLabel(workspace)}.`;
        setTimeout(() => selectors.loginDialog.close(), 800);
        toast(`Conta conectada em ${workspaceLabel(workspace)}.`, "success");
        if (workspace === "system") shareCodexAccount({silent:true}).catch(error => toast(error.message, "error"));
      } else if (params.error && state.loginWorkspace === workspace) el("login-status").textContent = params.error;
    } else if (method === "account/rateLimits/updated") loadRateLimits(workspaceGroup(rawWorkspace));
    return;
  }
  if (params.threadId && params.threadId !== state.activeThreadId) {
    addActivity(method, summarizeEvent(params));
    if (["thread/status/changed", "turn/completed"].includes(method)) loadThreads();
    return;
  }
  switch (method) {
    case "item/started":
    case "item/completed":
      clearLocalActivity();
      if (params.item?.id) state.items.set(params.item.id, {...state.items.get(params.item.id), ...params.item});
      renderMessages();
      addActivity(method, summarizeEvent(params.item || params));
      break;
    case "item/agentMessage/delta": appendDelta(params.itemId, "agentMessage", "_streamText", params.delta); break;
    case "item/plan/delta": appendDelta(params.itemId, "plan", "_streamText", params.delta); break;
    case "item/reasoning/summaryTextDelta": appendDelta(params.itemId, "reasoning", "_streamText", params.delta); break;
    case "item/commandExecution/outputDelta": appendDelta(params.itemId, "commandExecution", "_output", params.delta); break;
    case "turn/started":
      clearThreadTerminalStatus(eventThreadId || params.turn?.threadId || state.activeThreadId);
      state.activeTurnId = params.turn?.id || state.activeTurnId;
      state.activeTurnStartedAt = Date.parse(params.turn?.startedAt || params.turn?.started_at || "") || Date.now();
      state.executionActivityAt = Date.now();
      updateRunningUI();
      addActivity(method, `Turno ${state.activeTurnId || ""} iniciado`);
      break;
    case "turn/completed":
      clearLocalActivity();
      state.activeTurnId = null;
      if (["failed", "error"].includes(String(params.turn?.status || "").toLowerCase()) || params.turn?.error) {
        const rawError = params.turn?.error;
        const detail = typeof rawError === "string" ? rawError : rawError?.message || "A execução terminou com falha.";
        const errorId = `process-ended-${eventThreadId || state.activeThreadId || crypto.randomUUID()}`;
        state.items.set(errorId, {id:errorId, type:"error", message:processEndedMessage(detail)});
      }
      updateRunningUI();
      if (["failed", "error"].includes(String(params.turn?.status || "").toLowerCase()) || params.turn?.error) setStatus("error", "Processo encerrado");
      addActivity(method, params.turn?.status || "concluído");
      loadThreads();
      loadRateLimits(workspaceGroup(rawWorkspace));
      break;
    case "turn/diff/updated": state.diff = params.diff || ""; renderDiff(); break;
    case "clc/turnWatchdog": {
      const detail = params.browserCall
        ? "Uma operação do navegador deixou de responder e foi interrompida automaticamente."
        : "O turno deixou de produzir atividade e foi interrompido automaticamente.";
      recordThreadTerminalStatus(params.threadId || eventThreadId, processEndedMessage(detail));
      state.activeTurnId = null;
      clearLocalActivity();
      const watchdogId = `turn-watchdog-${params.turnId || crypto.randomUUID()}`;
      state.items.set(watchdogId, {id:watchdogId, type:"error", message:processEndedMessage(detail)});
      renderMessages(); updateRunningUI(); setStatus("error", "Turno recuperado pelo watchdog");
      addActivity("Recuperação automática", detail, "error");
      break;
    }
    case "thread/name/updated": if (params.name) selectors.title.textContent = conversationTitle({name:params.name, _projectName:state.activeProject?.name, _projectKind:state.activeProject?.kind}); loadThreads(); break;
    case "thread/archived":
    case "thread/deleted": if (params.threadId === state.activeThreadId) newThread(); loadThreads(); break;
    case "account/updated":
    case "account/login/completed":
      loadAccount({}, workspaceGroup(rawWorkspace));
      if (method === "account/login/completed" && params.success) {
        el("login-status").textContent = `Autenticação concluída em ${workspaceLabel(workspace)}.`;
        setTimeout(() => selectors.loginDialog.close(), 800);
        toast(`Conta conectada em ${workspaceLabel(workspace)}.`, "success");
        if (workspace === "system") shareCodexAccount({silent:true}).catch(error => toast(error.message, "error"));
        loadModels(workspaceGroup(rawWorkspace));
        if (state.setup.active) refreshSetupState();
      } else if (params.error) el("login-status").textContent = params.error;
      break;
    case "account/rateLimits/updated":
      loadRateLimits(workspaceGroup(rawWorkspace));
      break;
    case "error":
      clearLocalActivity();
      state.items.set(`error-${crypto.randomUUID()}`, {id:crypto.randomUUID(), type:"error", message:params.error?.message || "Erro no Codex"});
      renderMessages(); addActivity(method, summarizeEvent(params), "error"); break;
    default: if (/warning/i.test(method)) addActivity(method, summarizeEvent(params), "error");
  }
}

function appendDelta(itemId, type, field, delta) {
  if (!itemId || !delta) return;
  const item = state.items.get(itemId) || {id:itemId, type};
  item[field] = (item[field] || "") + delta;
  if (["agentMessage", "plan", "reasoning"].includes(type)) item.text = item[field];
  state.items.set(itemId, item);
  renderMessages();
}

function completedTurnUserAction(turn = {}) {
  const message = [...(turn.items || [])].reverse().find(item => item?.type === "agentMessage" && String(item.text || "").trim());
  const text = String(message?.text || "").replace(/\s+/g, " ").trim();
  if (!text) return null;
  const tail = text.slice(-900);
  const lowered = tail.toLocaleLowerCase("pt-BR");
  if (/.{0,180}(?:posso ajudar em mais alguma coisa|quer mais alguma coisa|se quiser,? posso ajudar)[?!. ]*$/.test(lowered)) return null;
  const explicit = /\b(?:autoriza|autorize|confirme|confirma|escolha|responda|informe|envie|anexe|conclua|faça o login|faça login|digite|selecione|preciso (?:que você|da sua)|aguardo (?:sua|a sua))\b/.test(lowered);
  const question = tail.includes("?") && /\b(?:qual|quais|quando|onde|como|você|vocês|prefere|deseja|quer|pode|autoriza)\b/.test(lowered);
  if (!explicit && !question) return null;
  if (/login|log in|sign in|autentic/.test(lowered)) return "Concluir autenticação";
  if (/navegador|browser|playwright/.test(lowered)) return "Responder sobre o navegador";
  if (/desktop|interface|mouse|teclado|tela/.test(lowered)) return "Responder sobre a interface";
  if (/escolha|selecione|qual|quais|prefere/.test(lowered)) return "Escolher uma opção";
  if (/envie|anexe/.test(lowered)) return "Enviar informação";
  if (/autoriza|autorize|confirme|confirma/.test(lowered)) return "Confirmar ação";
  return "Responder ao Codex";
}

function approvalTitle(method, params) {
  if (method === "item/commandExecution/requestApproval") return params.reason || "Executar comando";
  if (method === "item/fileChange/requestApproval") return params.reason || "Alterar arquivos";
  if (method === "item/permissions/requestApproval") return params.reason || "Conceder permissões";
  if (method === "mcpServer/elicitation/request") return params.message || "Responder à ferramenta";
  if (method === "item/tool/requestUserInput") return params.questions?.[0]?.header || "Sua resposta é necessária";
  return method;
}

function userInputQuestionsHTML(approval) {
  const key = `${approval.workspace || "system"}:${String(approval.id)}`;
  return (approval.params.questions || []).map(question => {
    const name = `user-input-${key}-${question.id}`;
    const options = (question.options || []).map(option => `<label class="user-input-option"><input type="radio" name="${escapeHTML(name)}" value="${escapeHTML(option.label)}" data-user-input-option="${escapeHTML(question.id)}"><span><strong>${escapeHTML(option.label)}</strong><small>${escapeHTML(option.description || "")}</small></span></label>`).join("");
    const freeInput = question.isOther || !question.options?.length
      ? `<label class="user-input-free"><span>${question.options?.length ? "Outra resposta" : "Resposta"}</span><input ${question.isSecret ? 'type="password"' : 'type="text"'} data-user-input-free="${escapeHTML(question.id)}" autocomplete="off"></label>`
      : "";
    return `<fieldset class="user-input-question"><legend>${escapeHTML(question.header || "Resposta")}</legend><p>${escapeHTML(question.question || "")}</p>${options}${freeInput}</fieldset>`;
  }).join("");
}

function approvalCardHTML(approval, inline = false) {
  const {method, params} = approval;
  const title = approvalTitle(method, params);
  let details = "";
  if (method === "item/commandExecution/requestApproval") details = commandText(params.command) || JSON.stringify(params.networkApprovalContext || {}, null, 2);
  else if (method === "item/fileChange/requestApproval") details = params.reason || params.grantRoot || "O Codex deseja aplicar alterações.";
  else if (method !== "item/tool/requestUserInput") details = JSON.stringify(params.permissions || params.requestedSchema || params, null, 2);
  const questionApproval = method === "item/tool/requestUserInput";
  const cls = `${inline ? "message-card tool approval-inline" : "approval-panel-card"} ${questionApproval ? "approval-input-card" : "approval-detail-card"}`;
  const detailLabel = method === "item/commandExecution/requestApproval" ? "Ver comando completo" : "Ver detalhes";
  const content = questionApproval
    ? `<div class="approval-input-scroll">${userInputQuestionsHTML(approval)}</div>`
    : `<details class="approval-details"><summary>${detailLabel}</summary><pre class="tool-output">${escapeHTML(details)}</pre></details>`;
  const timeout = method === "mcpServer/elicitation/request" ? "5 minutos" : method === "item/tool/requestUserInput" ? "60 minutos" : "15 minutos";
  return `<article class="${cls}" data-approval-key="${escapeHTML(`${approval.workspace || "system"}:${String(approval.id)}`)}" data-approval-id="${escapeHTML(String(approval.id))}"><div class="message-meta"><strong>${escapeHTML(title)}</strong><span title="${escapeHTML(method)}">${escapeHTML(workspaceLabel(approval.workspace))} • ${escapeHTML(userActionLabel(approval))}</span></div>${content}<small class="approval-timeout-note">Expira em ${timeout} sem resposta.</small><div class="approval-actions">${approvalButtons(approval)}</div></article>`;
}

function approvalTypeKey(approval) {
  const method = String(approval?.method || "");
  const params = approval?.params || {};
  if (method === "mcpServer/elicitation/request") {
    const message = String(params.message || "");
    const parsed = message.match(/Allow the\s+(.+?)\s+MCP server\s+to run tool\s+["']([^"']+)["']/i);
    const server = String(params.serverName || params.server || parsed?.[1] || "mcp").trim().toLowerCase();
    const tool = String(params.toolName || params.tool || parsed?.[2] || "request").trim().toLowerCase();
    return `${method}:${server}:${tool}`;
  }
  if (method === "item/commandExecution/requestApproval") {
    const command = commandText(params.command).trim().split(/\s+/)[0] || "command";
    return `${method}:${command}`;
  }
  if (method === "item/fileChange/requestApproval") return `${method}:${String(params.grantRoot || "files")}`;
  if (method === "item/permissions/requestApproval") return `${method}:${Object.keys(params.permissions || {}).sort().join(",")}`;
  return method;
}

function conversationApprovalRuleKey(approval) {
  const workspace = String(approval?.workspace || "system");
  const threadId = approvalThreadId(approval);
  return threadId ? `${workspace}:${threadId}:${approvalTypeKey(approval)}` : "";
}

function rememberConversationApprovalRule(approval) {
  const key = conversationApprovalRuleKey(approval);
  if (!key) return;
  state.conversationApprovalRules.add(key);
  const rules = [...state.conversationApprovalRules].slice(-500);
  state.conversationApprovalRules = new Set(rules);
  localStorage.setItem(CONVERSATION_APPROVAL_RULES_KEY, JSON.stringify(rules));
}

function automaticConversationApprovalAction(approval) {
  if (!state.conversationApprovalRules.has(conversationApprovalRuleKey(approval))) return "";
  if (approval.method === "item/permissions/requestApproval") return "grant";
  if (["item/commandExecution/requestApproval", "item/fileChange/requestApproval"].includes(approval.method)) return "acceptForSession";
  if (approval.method === "mcpServer/elicitation/request") return "accept-elicitation";
  return "";
}

function approvalButtons(approval) {
  const id = escapeHTML(`${approval.workspace || "system"}:${String(approval.id)}`);
  if (approval.method === "item/permissions/requestApproval") return `<button class="primary-button" data-approval="${id}" data-action="grant">Conceder solicitado</button><button class="secondary-button" data-approval="${id}" data-action="grant-all">Aprovar todas deste tipo</button><button class="danger-button" data-approval="${id}" data-action="deny-permissions">Negar</button>`;
  if (["item/commandExecution/requestApproval", "item/fileChange/requestApproval"].includes(approval.method)) return `<button class="primary-button" data-approval="${id}" data-action="accept">Aprovar uma vez</button><button class="secondary-button" data-approval="${id}" data-action="acceptForSession">Aprovar todas deste tipo</button><button class="danger-button" data-approval="${id}" data-action="decline">Recusar</button>`;
  if (approval.method === "mcpServer/elicitation/request") return `<button class="primary-button" data-approval="${id}" data-action="accept-elicitation">Aprovar</button><button class="secondary-button" data-approval="${id}" data-action="accept-all-elicitation">Aprovar todas deste tipo</button><button class="danger-button" data-approval="${id}" data-action="cancel-elicitation">Cancelar solicitação</button>`;
  if (approval.method === "item/tool/requestUserInput") return `<button class="primary-button" data-approval="${id}" data-action="submit-user-input">Responder e continuar</button><button class="danger-button" data-approval="${id}" data-action="cancel-user-input">Cancelar solicitação</button>`;
  return `<button class="danger-button" data-approval="${id}" data-action="cancel">Cancelar</button>`;
}

function renderApprovals() {
  const pending = [...state.approvals.values()].filter(item => !item.resolved && approvalBelongsToActiveThread(item));
  selectors.approvalCount.textContent = String(pending.length);
  selectors.approvalList.innerHTML = pending.length ? pending.map(item => approvalCardHTML(item)).join("") : '<div class="panel-empty">Nenhuma aprovação pendente.</div>';
  renderOtherConversationApprovals();
}

function otherConversationApprovalHTML(key, approval) {
  const threadId = approvalThreadId(approval);
  const thread = findApprovalThread(approval);
  const projectName = thread?._projectName || workspaceLabel(approval.workspace || "system");
  const threadName = thread ? conversationTitle(thread) : (threadId ? `Conversa ${threadId.slice(0, 8)}…` : "Origem não identificada");
  const requestTitle = approvalTitle(approval.method, approval.params || {});
  return `<article class="other-conversation-approval-popup" data-other-approval-key="${escapeHTML(key)}">
    <div class="other-approval-popup-header"><span>${escapeHTML(userActionLabel(approval))}</span><button type="button" class="secondary-button" data-open-approval-origin="${escapeHTML(key)}">Abrir</button></div>
    <strong title="${escapeHTML(threadName)}">${escapeHTML(threadName)}</strong>
    <small title="${escapeHTML(`${projectName} • ${requestTitle}`)}">${escapeHTML(projectName)} • ${escapeHTML(requestTitle)}</small>
  </article>`;
}

function renderOtherConversationApprovals() {
  if (!selectors.otherApprovalPopups) return;
  const pending = [...state.approvals.entries()]
    .filter(([, approval]) => !approval.resolved && !approvalBelongsToActiveThread(approval));
  selectors.otherApprovalPopups.innerHTML = pending.map(([key, approval]) => otherConversationApprovalHTML(key, approval)).join("");
}

async function openApprovalOrigin(key) {
  const approval = state.approvals.get(String(key));
  const threadId = approvalThreadId(approval);
  if (!approval || !threadId) return toast("A conversa de origem não foi informada por esta solicitação.", "error");
  let thread = findApprovalThread(approval);
  if (!thread && !state.activeProject) {
    await loadThreads().catch(() => null);
    thread = findApprovalThread(approval);
  }
  const rawWorkspace = String(approval.workspace || "system");
  const projectId = thread?._projectId || (rawWorkspace === "system" ? "system-control" : rawWorkspace.startsWith("project:") ? rawWorkspace.slice("project:".length) : "");
  if (!projectId) return toast("O projeto da conversa de origem ainda não está disponível.", "error");
  if (state.activeProject?.id !== projectId) await selectProject(projectId);
  await openThread(threadId);
}

async function respondApproval(key, action, options = {}) {
  const approval = state.approvals.get(String(key));
  if (!approval) return;
  const payload = {request_id:approval.id, workspace:approval.workspace || "system"};
  if (["grant", "grant-all"].includes(action)) payload.result = {scope:"turn", permissions:approval.params.permissions || {}};
  else if (action === "deny-permissions") payload.result = {scope:"turn", permissions:{}};
  else if (["accept-elicitation", "accept-all-elicitation"].includes(action)) payload.result = {action:"accept", content:{}};
  else if (action === "cancel-elicitation") payload.result = {action:"cancel", content:null};
  else if (["submit-user-input", "cancel-user-input"].includes(action)) {
    const answers = {};
    if (action === "submit-user-input") {
      const cards = [...document.querySelectorAll(`[data-approval-key="${CSS.escape(String(key))}"]`)];
      for (const question of approval.params.questions || []) {
        const selected = cards.map(card => card.querySelector(`[data-user-input-option="${CSS.escape(question.id)}"]:checked`)?.value || "").find(Boolean) || "";
        const free = cards.map(card => card.querySelector(`[data-user-input-free="${CSS.escape(question.id)}"]`)?.value.trim() || "").find(Boolean) || "";
        const values = [selected, free].filter(Boolean);
        if (!values.length) return toast(`Responda: ${question.header || question.question}`, "error");
        answers[question.id] = {answers:values};
      }
    }
    payload.result = {answers};
  }
  else payload.decision = action;
  try {
    if (["grant-all", "acceptForSession", "accept-all-elicitation"].includes(action)) rememberConversationApprovalRule(approval);
    await api("/api/approvals/respond", {method:"POST", body:JSON.stringify(payload)});
    approval.resolved = true;
    const requestThreadId = String(approval.params?.threadId || "");
    syncThreadWaitingStatus(requestThreadId);
    renderApprovals(); updateRunningUI();
    if (!options.automatic) {
      toast(action === "submit-user-input" ? "Resposta enviada." : action.startsWith("accept") || action.startsWith("grant") ? (["grant-all", "acceptForSession", "accept-all-elicitation"].includes(action) ? "Este tipo foi aprovado para a conversa." : "Operação aprovada.") : "Operação recusada.");
    }
  } catch (error) { toast(error.message, "error"); }
}

function renderDiff() {
  if (!state.diff) {
    selectors.diffView.textContent = "Nenhuma alteração registrada nesta conversa.";
    return;
  }
  selectors.diffView.innerHTML = state.diff.split("\n").map(line => {
    let cls = "";
    if (line.startsWith("+") && !line.startsWith("+++")) cls = "diff-line-add";
    if (line.startsWith("-") && !line.startsWith("---")) cls = "diff-line-del";
    return `<span class="${cls}">${escapeHTML(line)}</span>`;
  }).join("\n");
}

function summarizeEvent(value) {
  if (typeof value === "string") return value;
  try {
    const text = JSON.stringify(value, null, 2);
    return text.length > 1200 ? `${text.slice(0, 1200)}…` : text;
  } catch { return String(value); }
}

function addActivity(title, detail, type = "") {
  state.activity.unshift({title, detail, type, at:Date.now()});
  state.activity = state.activity.slice(0, 100);
  selectors.activityList.innerHTML = state.activity.length ? state.activity.map(item => `<article class="activity-card ${escapeHTML(item.type)}"><strong>${escapeHTML(item.title)}</strong><small>${formatTime(item.at)}</small><pre>${escapeHTML(item.detail)}</pre></article>`).join("") : '<div class="panel-empty">Nenhuma atividade.</div>';
}

function setStatus(kind, text) {
  selectors.status.className = `status-pill ${kind}`;
  selectors.status.textContent = text;
}

// ---------------------------------------------------------------------------
// Graphical settings and maintenance
// ---------------------------------------------------------------------------

function renderSettings(data) {
  if (!data || !selectors.settingsContent) return;
  const codex = data.codex || {};
  const tailscale = data.tailscale || {};
  const service = data.service || {};
  const security = data.security || {};
  const remoteDesktop = data.remote_desktop || {};
  const upstream = data.upstream || {};
  const upstreamCurrent = upstream.current || {};
  const upstreamDiff = upstream.diff || {};
  const upstreamSchema = upstreamCurrent.schema || {};
  const upstreamDesktop = upstreamCurrent.desktop || {};
  const upstreamCapabilityIds = upstreamCurrent.capability_ids || {};
  const upstreamCapabilityCount = Object.values(upstreamCapabilityIds).reduce((total, values) => total + (Array.isArray(values) ? values.length : 0), 0);
  const upstreamChangeCount = (upstreamDiff.methods_added || []).length + (upstreamDiff.methods_removed || []).length;
  const cloud = data.cloud_sync || {};
  const backupCloud = data.backup_cloud || {};
  const control = data.control || {};
  const backupStatus = String(control.backup?.status || "not-configured");
  const backupReady = backupCloud.configured || ["configured", "running", "complete", "warning"].includes(backupStatus);
  const backupDestination = backupCloud.configured
    ? `${escapeHTML(backupCloud.provider_label || "OneDrive")} conectado em identidade separada`
    : backupReady
      ? "Repositório local criptografado ativo; cópia externa pendente de autorização"
      : "Configure um destino independente para os backups criptografados";
  const machine = control.system || {};
  const resources = control.resources || {};
  const provision = control.provision || {};
  const memory = machine.memory || {};
  const rootDisk = (machine.filesystems || []).find(item => item.path === "/") || {};
  const hottest = (machine.temperatures || []).reduce((best, item) => Number(item.celsius || 0) > Number(best.celsius || 0) ? item : best, {});
  const resourceMode = resources.policy?.mode || "automatic";
  const resourceProfile = resources.state?.profile || resources.detected_profile || "balanced";
  const vm = machine.vm || {};
  const vmResources = control.vm_resources || {};
  const local = state.identity === "localhost";
  const systemAccount = state.accounts.system;
  const projectsAccount = state.accounts.projects;
  const accountLabel = (value, bridge) => value?.account?.email || (value?.account || (bridge?.running && bridge?.initialized) ? "Sessão ativa" : "Não conectada");
  const cloudflareIdentity = String(state.identity || "").startsWith("cloudflare:");
  const systemBridge = state.bridges.system || {};
  const projectsBridge = state.bridges.projects || {};
  const workers = machine.workers || control.workers || {};
  const gate = resources.game_gate || resources.state?.game_gate || machine.game_gate || {};
  const steam = machine.steam || {};
  const gameStorage = control.game_storage || {};
  const emulation = control.emulation || {};
  const recovery = control.recovery?.local || control.recovery || {};
  const recoveryCredentials = control.recovery?.credentials || {};
  const physical = control.physical?.sessions || {};
  const powerPolicy = control.power_policy || {};
  const hostAdmin = control.host_admin || {};
  const authd = control.authd || {};
  const serverControl = control.server || {};
  const publication = control.publication || {};
  const watchdog = control.watchdog || {};
  const watchdogHealth = watchdog.health || {};
  const watchdogSafe = watchdog.safe_mode || {};
  const watchdogQuarantine = watchdog.quarantine || {};
  const watchdogDevice = (watchdog.devices || []).find(item => item.available && !item.software) || (watchdog.devices || []).find(item => item.available) || {};
  const watchdogProtection = watchdog.hardware_watchdog ? "watchdog de hardware" : watchdog.software_fallback ? "softdog + kernel" : watchdog.configured ? "kernel/systemd" : "não configurado";
  const workerProjects = control.workers?.projects || {};
  const devices = state.devices || [];
  const enrollmentRequests = state.enrollmentRequests || [];
  const deviceAdmin = Boolean(state.deviceAdmin);
  const deviceLimit = Number(state.deviceLimit || 6);
  const deviceRows = devices.length ? devices.map(device => `
    <div class="settings-project-row device-row">
      <div class="project-copy"><strong>${escapeHTML(device.name)}</strong><small>${escapeHTML(device.identity || "")}</small><small>Chave: ${escapeHTML(device.fingerprint || "")}</small><small>Último acesso: ${escapeHTML(formatTime(device.last_seen_at || device.created_at))}</small>${(device.access_history || []).slice(0, 5).map(entry => `<small>${escapeHTML(entry.event === "authenticated" ? "Acesso" : entry.event === "registered" ? "Cadastro" : "Revogação")} • ${escapeHTML(entry.email || "")} • ${escapeHTML(formatTime(entry.at))}${entry.ip ? ` • IP ${escapeHTML(entry.ip)}` : ""}</small>`).join("")}</div>
      <button class="danger-button compact-button" data-device-revoke="${escapeHTML(device.id)}">Revogar</button>
    </div>`).join("") : '<div class="inline-notice">Nenhum dispositivo remoto cadastrado.</div>';
  const enrollmentRows = enrollmentRequests.length ? enrollmentRequests.map(request => `
    <div class="settings-project-row device-row">
      <div class="project-copy"><strong>${escapeHTML(request.name)}</strong><small>${escapeHTML(request.email || request.identity || "")}</small><small>Solicitado: ${escapeHTML(formatTime(request.created_at))} • ${escapeHTML(request.client_ip || "IP indisponível")}</small><small>Chave: ${escapeHTML(request.fingerprint || "")}</small></div>
      <div class="card-actions"><button class="primary-button compact-button" data-enrollment-approve="${escapeHTML(request.id)}">Aprovar</button><button class="danger-button compact-button" data-enrollment-reject="${escapeHTML(request.id)}">Reprovar</button></div>
    </div>`).join("") : '<div class="inline-notice">Nenhuma solicitação pendente.</div>';
  selectors.settingsContent.innerHTML = `
    <nav class="settings-category-nav" aria-label="Categorias das configurações">
      <button type="button" data-settings-jump="system-control" data-settings-title="Servidor"><span class="category-icon">▣</span><span><strong>Servidor</strong><small>Mini PC, VM e recursos</small></span></button>
      <button type="button" data-settings-jump="sites-control" data-settings-title="Sites"><span class="category-icon">◎</span><span><strong>Sites</strong><small>Publicação sasocq.com</small></span></button>
      <button type="button" data-settings-jump="remote-access" data-settings-title="Acesso remoto"><span class="category-icon">↗</span><span><strong>Acesso remoto</strong><small>Desktop e navegador</small></span></button>
      <button type="button" data-settings-jump="steam-control" data-settings-title="Steam e jogos"><span class="category-icon">◈</span><span><strong>Steam e jogos</strong><small>HDMI, biblioteca e discos</small></span></button>
      <button type="button" data-settings-jump="codex-account" data-settings-title="Codex"><span class="category-icon">›_</span><span><strong>Codex</strong><small>Conta e ferramentas</small></span></button>
      <button type="button" data-settings-jump="projects-control" data-settings-title="Projetos"><span class="category-icon">◇</span><span><strong>Projetos</strong><small>Workers e sincronização</small></span></button>
      <button type="button" data-settings-jump="backup-recovery" data-settings-title="Backup e recuperação"><span class="category-icon">↻</span><span><strong>Backup</strong><small>Proteção e recuperação</small></span></button>
      <button type="button" data-settings-jump="security-control" data-settings-title="Segurança"><span class="category-icon">✓</span><span><strong>Segurança</strong><small>Identidade e dispositivos</small></span></button>
      <button type="button" data-settings-jump="diagnostics-control" data-settings-title="Aplicativo e diagnóstico"><span class="category-icon">⋯</span><span><strong>Aplicativo</strong><small>Atualização e diagnóstico</small></span></button>
    </nav>
    <header class="settings-subpage-header hidden"><button type="button" class="secondary-button" data-settings-home>← Categorias</button><div><small>Configurações</small><strong id="settings-subpage-title"></strong></div></header>
    <section id="system-control" class="settings-section">
      <div class="settings-section-title"><div><h3>Sistema SASOCQ</h3><p>Controle permanente do host, servidor, recursos, Steam e recuperação.</p></div><span class="value">${control.available ? "Broker local ativo" : "Broker indisponível"}</span></div>
      <div class="maintenance-grid">
        <article class="maintenance-card"><h3>Mini PC</h3><p>${escapeHTML(machine.hostname || "Aguardando dados")}</p><span class="value">CPU ${escapeHTML(machine.cpu?.percent ?? "—")}% • ${escapeHTML(machine.cpu?.logical || "—")} threads${hottest.celsius ? ` • ${escapeHTML(hottest.celsius)} °C` : ""}</span></article>
        <article class="maintenance-card"><h3>Memória e SSD</h3><p>${formatBytes(memory.used)} de ${formatBytes(memory.total)} em uso</p><span class="value">SSD: ${formatBytes(rootDisk.free)} livres • ${escapeHTML(rootDisk.percent ?? "—")}% usado</span></article>
        <article class="maintenance-card"><h3>Servidor Ubuntu VM</h3><p>${escapeHTML(vm.state || vmResources.state || "não criado")} • execução permanente, monitorada automaticamente</p><span class="value">sasocq-server • atual ${escapeHTML(vmResources.current?.memory_mib || "—")} MiB/${escapeHTML(vmResources.current?.vcpus || "—")} vCPU • recomendado ${escapeHTML(vmResources.recommended?.memory_mib || "—")} MiB/${escapeHTML(vmResources.recommended?.vcpus || "—")} vCPU</span>${vmResources.available && vmResources.configuration_mismatch ? `<div class="card-actions"><button class="secondary-button" data-settings-action="vm-reconcile" ${!control.available ? "disabled" : ""}>Adaptar recursos recomendados</button></div>` : ""}</article>
        <article class="maintenance-card"><h3>Distribuição de capacidade</h3><p>Modo ${escapeHTML(resourceMode)} • perfil aplicado ${escapeHTML(resourceProfile)}</p><span class="value">Servidor e painel mantêm prioridade; workers cedem ao jogo.</span><div class="card-actions"><button class="secondary-button" data-settings-action="resources-automatic">Automático</button><button class="secondary-button" data-settings-action="resources-game">Jogo</button><button class="secondary-button" data-settings-action="resources-codex">Codex</button><button class="secondary-button" data-settings-action="resources-server">Servidor</button></div></article>
        <article class="maintenance-card"><h3>Autorrecuperação do host</h3><p>${watchdogHealth.healthy === false ? "Recuperando componentes críticos" : watchdogSafe.active ? "Modo seguro após falha" : watchdog.configured ? "Proteção automática ativa" : "Proteção ainda não configurada"}</p><span class="value">${escapeHTML(watchdogProtection)}${watchdogDevice.identity ? ` • ${escapeHTML(watchdogDevice.identity)}` : ""} • servidor e Control Plane retornam automaticamente; jogo anterior não reabre${watchdogQuarantine.workers_quarantined ? " • workers suspeitos em quarentena" : ""}</span><div class="card-actions"><button class="secondary-button" data-settings-action="watchdog-check" ${!control.available ? "disabled" : ""}>Verificar agora</button><button class="secondary-button" data-settings-action="watchdog-install" ${!control.available ? "disabled" : ""}>Reaplicar proteção</button>${watchdogQuarantine.active ? '<button class="primary-button" data-settings-action="watchdog-clear-quarantine">Liberar cargas em quarentena</button>' : ""}<button class="danger-button" data-settings-action="watchdog-reboot-test" ${!control.available ? "disabled" : ""}>Testar recuperação por reboot</button></div></article>
        <article class="maintenance-card"><h3>Steam Machine / HDMI</h3><p>${steam.game_running ? "Jogo em execução" : steam.running ? "Processos do Steam ativos; interface pode precisar de reativação" : gate.allowed === false ? "Bloqueado até o hardware estabilizar" : "Pronto para joystick"}</p><span class="value">${gate.allowed === false ? escapeHTML((gate.reasons || []).join(" • ") || "Recursos insuficientes") : "Steam Big Picture centraliza a biblioteca; Heroic, Lutris e Bottles permanecem fechados quando não estão em uso"}</span><div class="card-actions"><button class="primary-button" data-settings-action="steam-start" ${!control.available || gate.allowed === false ? "disabled" : ""}>Reativar Big Picture</button><button class="secondary-button" data-settings-action="steam-sync-library" ${!control.available || gate.allowed === false ? "disabled" : ""}>Atualizar biblioteca</button><button class="secondary-button" data-settings-action="steam-open-heroic" ${!control.available || gate.allowed === false ? "disabled" : ""}>Entrar no Heroic</button><button class="secondary-button" data-settings-action="steam-open-lutris" ${!control.available || gate.allowed === false ? "disabled" : ""}>Abrir Lutris</button><button class="secondary-button" data-settings-action="steam-open-bottles" ${!control.available || gate.allowed === false ? "disabled" : ""}>Abrir Bottles</button><button class="danger-button" data-settings-action="steam-stop" ${!control.available ? "disabled" : ""}>Fechar jogo</button></div></article>
        <article class="maintenance-card"><h3>Disco externo de jogos</h3><p>${gameStorage.healthy ? `Pronto em ${escapeHTML(gameStorage.mountpoint || "/srv/games")}` : escapeHTML(gameStorage.message || "Conecte um SSD/HD externo")}</p><span class="value">${gameStorage.healthy ? `${formatBytes(gameStorage.usage?.free)} livres • UUID ${escapeHTML(gameStorage.uuid || "")}` : `${(gameStorage.candidates || []).length} disco(s) externo(s) detectado(s)`}</span><div class="card-actions"><button class="secondary-button" data-settings-action="game-storage-ensure" ${!control.available ? "disabled" : ""}>Verificar/montar</button><button class="primary-button" data-settings-action="game-storage-adopt" ${!control.available || !(gameStorage.candidates || []).some(d => (d.partitions || []).some(p => p.uuid)) ? "disabled" : ""}>Usar volume existente</button><button class="danger-button" data-settings-action="game-storage-prepare" ${!control.available || !(gameStorage.candidates || []).length ? "disabled" : ""}>Formatar disco externo</button></div></article>
        <article class="maintenance-card"><h3>Emuladores e biblioteca universal</h3><p>${emulation.ready ? `${escapeHTML(emulation.count || 0)} jogo(s) emulado(s) indexado(s)` : escapeHTML(emulation.message || "Aguardando armazenamento externo")}</p><span class="value">RetroArch, mGBA, Dolphin, PCSX2, RPCS3, PPSSPP, Cemu e adaptador Switch fornecido pelo usuário; nenhum ROM/firmware/chave proprietário é incluído.</span><div class="card-actions"><button class="primary-button" data-settings-action="emulation-install" ${!control.available || !gameStorage.healthy ? "disabled" : ""}>Instalar/atualizar emuladores</button><button class="secondary-button" data-settings-action="emulation-scan" ${!control.available || !gameStorage.healthy ? "disabled" : ""}>Indexar e enviar ao Steam</button><button class="secondary-button" data-settings-action="steam-sync-library" ${!control.available || !gameStorage.healthy ? "disabled" : ""}>Sincronizar tudo</button></div></article>
        <article class="maintenance-card"><h3>Codex Workers</h3><p>${workers.running || workers.processes?.length ? "Projetos em execução isolada" : "Nenhum worker pesado ativo"}</p><span class="value">Projetos administram a VM e sasocq.com; host permanece no Control Plane.</span><div class="card-actions"><button class="secondary-button" data-settings-action="workers-resume" ${!control.available ? "disabled" : ""}>Retomar</button><button class="secondary-button" data-settings-action="workers-pause" ${!control.available ? "disabled" : ""}>Pausar</button><button class="danger-button" data-settings-action="workers-stop" ${!control.available ? "disabled" : ""}>Encerrar</button></div></article>
        <article class="maintenance-card"><h3>Backup e recuperação</h3><p>${backupDestination}</p><span class="value">Backup integral diário e imagem consistente da VM • última execução ${escapeHTML(backupStatus)} • restauração isolada ${control.backup?.status === "complete" ? "validada" : "pendente"} • recovery local ${recovery.available ? "pronto" : "requer pendrive"}</span><div class="cloud-form"><label class="field-label">Pasta escolhida para os backups no OneDrive<input id="backup-cloud-remote-path" type="text" value="${escapeHTML(backupCloud.remote_path || DEFAULT_BACKUP_REMOTE_PATH)}" readonly aria-readonly="true"><small>Use o explorador abaixo para alterar o destino. Atual: OneDrive / ${escapeHTML(backupCloud.remote_path || DEFAULT_BACKUP_REMOTE_PATH)}</small></label></div>${backupCloud.session?.question ? `<div class="cloud-form"><label class="field-label">${escapeHTML(backupCloud.session.question.Name || "Resposta")}${backupCloud.session.question.Examples?.length ? `<select id="backup-cloud-question-answer">${backupCloud.session.question.Examples.map(item => `<option value="${escapeHTML(item.Value)}">${escapeHTML(item.Help || item.Value)}</option>`).join("")}</select>` : `<input id="backup-cloud-question-answer" type="${backupCloud.session.question.IsPassword ? "password" : "text"}" value="${escapeHTML(backupCloud.session.question.Default ?? "")}" autocomplete="off">`}</label></div>` : ""}<div class="card-actions">${!backupCloud.installed ? `<button class="secondary-button" data-settings-action="backup-cloud-install" ${!local ? "disabled" : ""}>Instalar suporte</button>` : ""}${!backupCloud.configured && !backupCloud.session?.question ? '<button class="secondary-button" data-settings-action="backup-cloud-connect">Conectar OneDrive opcional</button>' : ""}${backupCloud.session?.question ? '<button class="primary-button" data-settings-action="backup-cloud-answer">Continuar login</button>' : ""}${backupCloud.configured ? '<button class="primary-button" data-settings-action="backup-cloud-browse">Abrir pastas do OneDrive</button>' : ""}<button class="primary-button" data-settings-action="backup-run" ${!control.available || !backupReady ? "disabled" : ""}>Executar backup</button><button class="secondary-button" data-settings-action="backup-restore" ${!control.available || !backupReady ? "disabled" : ""}>Restaurar servidor</button><button class="secondary-button" data-settings-action="recovery-prepare" ${!control.available ? "disabled" : ""}>Atualizar recovery local</button><button class="secondary-button" data-settings-action="recovery-repair" ${!control.available ? "disabled" : ""}>Reparar Ubuntu</button><button class="secondary-button" data-settings-action="recovery-reapply" ${!control.available ? "disabled" : ""}>Reaplicar configuração</button><button class="danger-button" data-settings-action="recovery-factory-reset" ${!control.available || !recovery.available ? "disabled" : ""}>Restaurar este PC</button><button class="secondary-button" data-settings-action="recovery-export" ${!control.available ? "disabled" : ""}>Exportar credenciais de emergência</button><button class="secondary-button" data-settings-action="provision-run" ${!control.available ? "disabled" : ""}>Verificar configuração</button></div></article>
        <article class="maintenance-card"><h3>Proteção de energia e telas físicas</h3><p>${powerPolicy.installed ? "Jogos, Desktop e workers não podem desligar, reiniciar nem suspender o host" : "Política de energia precisa ser aplicada"}</p><span class="value">Controle físico: Desktop ${physical.desktop?.available ? "disponível" : "indisponível"} • HDMI/Jogos ${physical.jogos?.available ? "disponível" : "indisponível"}</span><div class="card-actions"><button class="primary-button" data-settings-action="power-policy-install" ${!control.available || powerPolicy.installed ? "disabled" : ""}>Aplicar proteção</button><button class="secondary-button" data-settings-action="remote-physical-desktop" ${!physical.desktop?.available ? "disabled" : ""}>Controlar Desktop</button><button class="secondary-button" data-settings-action="remote-physical-games" ${!physical.jogos?.available ? "disabled" : ""}>Controlar HDMI/Steam</button></div></article>
        <article class="maintenance-card"><h3>Administração integral do host</h3><p>${hostAdmin.full_host_administration ? "Disponível ao Codex do Sistema por argv auditado, leitura e escrita atômica" : "Indisponível"}</p><span class="value">Sem shell root remoto persistente; ações destrutivas exigem nova autenticação Microsoft.</span></article>
        <article class="maintenance-card"><h3>Login Ubuntu pelo Authenticator</h3><p>${authd.configured ? `Configurado para ${escapeHTML(authd.configuration?.owner_email || "identidade Microsoft")}` : authd.available ? "authd instalado; falta vincular o aplicativo Microsoft Entra" : "Preparar login Microsoft no Ubuntu"}</p><span class="value">Conta de administração e conta(s) OneDrive permanecem independentes. A conta da área de trabalho não recebe sudo.</span><div class="cloud-form"><label class="field-label">Tenant/Directory ID<input id="authd-tenant-id" value="${escapeHTML(authd.configuration?.issuer?.match(/microsoftonline\.com\/([^/]+)/)?.[1] || state.lastStatus?.security?.entra?.tenant || "")}" autocomplete="off" placeholder="Directory (tenant) ID"></label><label class="field-label">Client ID do authd<input id="authd-client-id" value="${escapeHTML(authd.configuration?.client_id || state.lastStatus?.security?.entra?.client_id || "")}" autocomplete="off" placeholder="Application (client) ID"></label><label class="field-label full">E-mail proprietário<input id="authd-owner-email" value="${escapeHTML(authd.configuration?.owner_email || state.session?.entra?.email || "")}" autocomplete="email" placeholder="usuario@dominio.com"></label><label class="check-row"><input id="authd-register-device" type="checkbox" ${authd.configuration?.register_device ? "checked" : ""}><span>Registrar o mini PC no Microsoft Entra quando permitido pelo tenant</span></label></div><div class="card-actions"><button class="secondary-button" data-settings-action="authd-prepare" ${!control.available ? "disabled" : ""}>Instalar/preparar</button><button class="primary-button" data-settings-action="authd-configure" ${!control.available ? "disabled" : ""}>Vincular login Ubuntu</button></div></article>
        <article class="maintenance-card"><h3>Instalar programas</h3><p>Instalação ou remoção por nome de pacote, executada pelo Control Plane sem revelar senha de sudo.</p><div class="cloud-form"><label class="field-label full">Pacotes Ubuntu<input id="system-packages" autocomplete="off" placeholder="Ex.: vlc gimp inkscape"></label></div><span class="value">Instalar ou remover exige autenticação Microsoft recente. Aplicativos Flatpak de usuário podem ser instalados pela loja gráfica.</span><div class="card-actions"><button class="primary-button" data-settings-action="packages-install" ${!control.available ? "disabled" : ""}>Instalar</button><button class="danger-button" data-settings-action="packages-remove" ${!control.available ? "disabled" : ""}>Remover</button></div></article>
        <article class="maintenance-card"><h3>Publicação dos sites sasocq.com</h3><p>${publication.configured ? "Cloudflare Tunnel ativo no servidor" : "Conector público ainda não ativado"}</p><span class="value">${publication.tunnel?.origin_health === "ok" ? "Nginx/origem local saudável" : "Aguardando origem saudável"} • nenhuma porta administrativa é publicada</span><div class="cloud-form"><label class="field-label full">Hostnames configurados no painel Cloudflare<input id="publication-hostnames" autocomplete="off" value="${escapeHTML((publication.hostnames || ["sasocq.com"]).join(" "))}" placeholder="sasocq.com app.sasocq.com"></label><label class="field-label full">Token do Cloudflare Tunnel<input id="publication-token" type="password" autocomplete="new-password" placeholder="Cole o token do túnel remoto; ele será gravado somente na VM"></label></div><div class="card-actions"><button class="secondary-button" data-settings-action="publication-install" ${!control.available ? "disabled" : ""}>Instalar conector na VM</button><button class="primary-button" data-settings-action="publication-configure" ${!control.available ? "disabled" : ""}>Ativar publicação</button></div></article>
        <article class="maintenance-card"><h3>Próxima sessão no HDMI</h3><p>Escolha o usuário aberto automaticamente no próximo boot.</p><div class="card-actions"><button class="secondary-button" data-settings-action="session-games">Jogos</button><button class="secondary-button" data-settings-action="session-desktop">Desktop</button><button class="secondary-button" data-settings-action="session-codex">Codex</button></div></article>
        <article class="maintenance-card"><h3>Ubuntu</h3><p>${escapeHTML(machine.platform || "Sistema instalado")}</p><span class="value">Kernel ${escapeHTML(machine.kernel || "—")}</span><div class="card-actions"><button class="primary-button" data-settings-action="system-update" ${!control.available ? "disabled" : ""}>Atualizar sistema</button><button class="danger-button" data-settings-action="system-reboot" ${!control.available ? "disabled" : ""}>Reiniciar PC</button></div></article>
      </div>
      ${control.error ? `<div class="inline-notice error">${escapeHTML(control.error)}</div>` : ""}
    </section>

    <section id="application-control" class="settings-section">
      <div class="settings-section-title"><div><h3>Aplicativo</h3><p>Estado geral e inicialização.</p></div></div>
      <div class="maintenance-grid">
        <article class="maintenance-card"><h3>Versão</h3><p>Pacote instalado</p><span class="value">${escapeHTML(data.app?.version || state.session?.app_version || "0.9.0")}</span></article>
        <article class="maintenance-card"><h3>Identidade atual</h3><p>Quem abriu esta sessão</p><span class="value">${escapeHTML(state.identity || "—")}</span></article>
        <article class="maintenance-card switch-row"><div><h3>Iniciar automaticamente</h3><p>Mantém o acesso pronto depois de entrar no Linux.</p></div><label class="switch"><input id="settings-autostart" type="checkbox" ${service.enabled ? "checked" : ""}><span></span></label></article>
        <article class="maintenance-card"><h3>Serviço</h3><p>${service.active ? "Executando normalmente" : "Não aparece ativo"}</p><div class="card-actions"><button class="secondary-button" data-settings-action="restart-app">Reiniciar aplicativo</button></div></article>
      </div>
    </section>

    <section id="codex-account" class="settings-section">
      <div class="settings-section-title"><div><h3>Codex e conta</h3><p>Instalação, atualização e autenticação sem terminal.</p></div></div>
      <div class="maintenance-grid">
        <article class="maintenance-card"><h3>${codex.installed ? "Codex instalado" : "Codex ausente"}</h3><p>${escapeHTML(codex.path || "Instale pelo botão")}</p><span class="value">${escapeHTML(codex.version || "—")}</span><div class="card-actions"><button class="primary-button" data-settings-action="update-codex">${codex.installed ? "Atualizar Codex" : "Instalar Codex"}</button></div></article>
        <article class="maintenance-card"><h3>Conta única do Codex</h3><p>${escapeHTML(accountLabel(systemAccount, systemBridge))}</p><span class="value">${systemAccount?.account ? "✓ Autenticado e conectado ao Control Plane; conta compartilhada com Projetos" : systemBridge.initialized ? "Control Plane pronto; login pendente" : escapeHTML(systemBridge.last_error || "Indisponível")}</span><div class="card-actions">${systemAccount?.account ? '<span class="account-connected" role="status">Login concluído</span><button class="danger-button" data-settings-action="logout-codex-system">Sair da conta única</button>' : `<button class="primary-button" data-settings-action="login-codex-system" ${!systemBridge.initialized ? "disabled" : ""}>Entrar no Codex</button>`}</div></article>
        <article class="maintenance-card"><h3>Codex de Projetos</h3><p>Usa a mesma conta do Codex do Sistema, mantendo processos, arquivos e permissões isolados.</p><span class="value">${projectsAccount?.account ? `✓ Autenticado; workers independentes conectados • ${escapeHTML(accountLabel(projectsAccount, projectsBridge))}` : systemAccount?.account ? "Conta do Sistema pronta para ser aplicada" : "Aguardando login na conta única"}</span><div class="card-actions">${systemAccount?.account ? `<button class="${projectsAccount?.account ? "secondary-button" : "primary-button"}" data-settings-action="share-codex-account">${projectsAccount?.account ? "Reaplicar conta única" : "Usar conta do Sistema"}</button>` : ""}</div></article>
        <article class="maintenance-card"><h3>Codex / Upstream</h3><p>${upstream.last_checked_at ? `Verificado em ${escapeHTML(formatTime(upstream.last_checked_at))}` : "Primeira verificação ainda não executada"}</p><span class="value">${escapeHTML(upstreamCurrent.codex_version || codex.version || "Codex detectado")} • schema ${upstreamSchema.ok ? escapeHTML(String(upstreamSchema.hash || "").slice(0, 12)) : "pendente"} • ${upstreamChangeCount} mudança(s)</span><div class="card-actions"><button class="primary-button" data-settings-action="check-codex-upstream">Verificar agora</button><a class="secondary-button" href="https://learn.chatgpt.com/docs/changelog" target="_blank" rel="noopener">Changelog oficial</a></div></article>
        <article class="maintenance-card"><h3>Capacidades descobertas</h3><p>${upstreamCapabilityCount} item(ns) em modelos, recursos experimentais, permissões, modos, skills e hooks.</p><span class="value">${upstreamSchema.methods?.length || 0} método(s) no protocolo • promoção automática desativada</span></article>
        <article class="maintenance-card"><h3>ChatGPT Desktop oficial</h3><p>${upstreamDesktop.installed ? `Instalado • ${escapeHTML(upstreamDesktop.version || "versão detectada")}` : "Não instalado neste mini PC"}</p><span class="value">Interface oficial usa os mesmos projetos e arquivos; recursos internos sem API continuam exclusivos do Desktop.</span><div class="card-actions"><button class="secondary-button" data-settings-action="open-chatgpt-desktop" ${!remoteDesktop.available ? "disabled" : ""}>Abrir sessão gráfica</button></div></article>
        ${codexUsageHTML(systemAccount || projectsAccount, state.rateLimits.system || state.rateLimits.projects)}
      </div>
    </section>

    <section id="remote-access" class="settings-section">
      <div class="settings-section-title"><div><h3>Área gráfica remota adaptativa</h3><p>Tela interativa para toque, teclado virtual, mouse e teclado físico.</p></div></div>
      <div class="maintenance-grid">
        <article class="maintenance-card"><h3>${remoteDesktop.available ? "Ambiente gráfico pronto" : "Componentes pendentes"}</h3><p>${escapeHTML(describeRemoteStatus(remoteDesktop))}</p><span class="value">${remoteDesktop.tcp_vnc_exposed === false ? "VNC sem porta TCP pública" : "Verifique a instalação"}</span><div class="card-actions"><button class="primary-button" data-settings-action="open-remote-desktop" ${!remoteDesktop.available ? "disabled" : ""}>Abrir tela</button><button class="secondary-button" data-settings-action="open-remote-browser" ${!remoteDesktop.available ? "disabled" : ""}>Navegador móvel</button>${remoteDesktop.running ? '<button class="danger-button" data-settings-action="stop-remote-desktop">Encerrar</button>' : ""}</div></article>
        <article class="maintenance-card"><h3>Adaptação automática</h3><p>Celular: toque, gestos, teclado virtual e perfil responsivo. Tablet e PC: resolução e escala próprias.</p><span class="value">${escapeHTML(remoteDesktop.transport || "WSS autenticado → socket Unix")}</span></article>
        <article class="maintenance-card"><h3>Navegador remoto</h3><p>Perfil persistente separado do navegador pessoal.</p><span class="value">${escapeHTML(remoteDesktop.browser_mode || "Modo automático")}</span></article>
        <article class="maintenance-card"><h3>Experiência completa</h3><p>${data.full_experience?.installed ? "Navegador, MCPs e desktop preparados" : "Instale os componentes necessários"}</p><div class="card-actions"><button class="primary-button" data-settings-action="install-full-experience" ${!local ? "disabled" : ""}>${data.full_experience?.installed ? "Reparar ou atualizar" : "Instalar tudo"}</button></div></article>
      </div>
    </section>

    <section id="tools-control" class="settings-section">
      <div class="settings-section-title"><div><h3>Ferramentas e plug-ins</h3><p>Skills, apps, MCP, navegador Playwright e controle supervisionado.</p></div><button class="secondary-button" data-settings-action="open-tools" ${!state.activeProject ? "disabled" : ""}>Gerenciar ferramentas</button></div>
      <div class="maintenance-grid">
        <article class="maintenance-card"><h3>Navegador Playwright</h3><p>${data.full_experience?.playwright?.installed ? "MCP instalado" : "Ainda não preparado"}</p><span class="value">${data.full_experience?.playwright?.enabled ? "Ativado" : "Desativado"}</span></article>
        <article class="maintenance-card"><h3>Controle do Linux</h3><p>${escapeHTML(data.full_experience?.desktop?.session_type || "sessão não detectada")} • ${escapeHTML(data.full_experience?.desktop?.input_backend || "entrada indisponível")}</p><span class="value">${data.full_experience?.desktop?.enabled ? "Ativado com aprovações" : "Desativado"}</span></article>
        <article class="maintenance-card"><h3>Uso por conversa</h3><p>Associe somente os recursos necessários à thread.</p><span class="value">${toolProfileCount()} selecionado(s)</span><div class="card-actions"><button class="secondary-button" data-settings-action="open-tools" ${!state.activeProject ? "disabled" : ""}>Abrir central</button></div></article>
      </div>
    </section>

    <section id="projects-control" class="settings-section">
      <div class="settings-section-title"><div><h3>Projetos</h3><p>Pastas às quais o Codex pode ter acesso.</p></div>${local ? '<button class="primary-button" data-settings-action="add-project">Escolher pasta</button>' : ""}</div>
      <div class="settings-project-list">${state.projects.length ? state.projects.map(project => { const worker = workerProjects[project.id] || {}; const props = worker.properties || {}; return `<div class="settings-project-row"><div class="project-copy"><strong>${escapeHTML(project.name)}</strong><small>${escapeHTML(project.path)}</small>${project.kind !== "system" ? `<small>Worker: ${escapeHTML(props.ActiveState || "inativo")} • prioridade ${escapeHTML(worker.priority || "normal")} • memória ${formatBytes(Number(props.MemoryCurrent || 0))}${worker.limits?.cpu_quota_percent ? ` • CPU máx. ${escapeHTML(worker.limits.cpu_quota_percent)}%` : ""}${worker.limits?.memory_max_mib ? ` • RAM máx. ${escapeHTML(worker.limits.memory_max_mib)} MiB` : ""}</small>` : ""}</div>${project.kind === "system" ? '<span class="value">Permanente</span>' : `<select data-worker-priority="${escapeHTML(project.id)}" title="Prioridade"><option value="background" ${worker.priority === "background" ? "selected" : ""}>Background</option><option value="normal" ${!worker.priority || worker.priority === "normal" ? "selected" : ""}>Normal</option><option value="high" ${worker.priority === "high" ? "selected" : ""}>Alta</option><option value="critical" ${worker.priority === "critical" ? "selected" : ""}>Crítica</option></select><button class="secondary-button compact-button" data-settings-action="worker-limits:${escapeHTML(project.id)}">Limites</button><button class="secondary-button compact-button" data-settings-action="worker-resume:${escapeHTML(project.id)}">Retomar</button><button class="secondary-button compact-button" data-settings-action="worker-pause:${escapeHTML(project.id)}">Pausar</button><button class="danger-button compact-button" data-settings-action="worker-stop:${escapeHTML(project.id)}">Encerrar</button><button class="icon-button" data-project-rename="${escapeHTML(project.id)}" title="Renomear">✎</button><button class="icon-button" data-project-delete="${escapeHTML(project.id)}" title="Remover">×</button>`}</div>`; }).join("") : '<div class="inline-notice">Nenhum projeto cadastrado.</div>'}</div>
      ${!local ? '<div class="inline-notice">Para escolher uma nova pasta, abra esta tela diretamente no computador Linux.</div>' : ""}
    </section>

    <section id="cloud-projects-control" class="settings-section">
      <div class="settings-section-title"><div><h3>Sincronização externa opcional</h3><p>Os projetos funcionam localmente sem nuvem; Google Drive ou OneDrive podem ser usados apenas como cópia sincronizada.</p></div>${local ? '<button class="primary-button" data-settings-action="configure-cloud">Configurar ou trocar</button>' : ""}</div>
      <div class="maintenance-grid">
        <article class="maintenance-card"><h3>${cloud.configured ? `${escapeHTML(cloud.provider_label || "Nuvem")} conectado` : cloud.installed && !cloud.compatible ? "Sincronizador precisa ser atualizado" : "Operação somente local"}</h3><p>${cloud.configured ? `${escapeHTML(cloud.remote_name || "remote")}:${escapeHTML(cloud.remote_path || "")}` : cloud.installed && !cloud.compatible ? `${escapeHTML(cloud.version || "Versão antiga")} • mínimo ${escapeHTML(cloud.minimum_version || "1.71.0")}` : "Nenhuma dependência de Google Cloud, Google Drive ou OneDrive"}</p><span class="value">${cloud.enabled ? "Sincronização automática ativa" : cloud.initialized ? "Sincronização pausada" : "Projetos locais disponíveis"}</span><div class="card-actions">${cloud.initialized ? '<button class="primary-button" data-settings-action="sync-cloud-now">Sincronizar agora</button>' : ""}${cloud.enabled ? '<button class="danger-button" data-settings-action="pause-cloud">Pausar</button>' : cloud.initialized ? '<button class="secondary-button" data-settings-action="resume-cloud">Retomar</button>' : ""}</div></article>
        <article class="maintenance-card"><h3>Pastas dos projetos</h3><p>Escolha a pasta local usada pelo Codex e a pasta correspondente no OneDrive ou Google Drive.</p><div class="cloud-form"><label class="field-label">Pasta local<input id="settings-cloud-local-path" type="text" value="${escapeHTML(cloud.local_path || "~/CodexProjects")}" autocomplete="off"></label><label class="field-label">Pasta na nuvem<input id="settings-cloud-remote-path" type="text" value="${escapeHTML(cloud.remote_path || "Codex Linux Control/Projetos")}" autocomplete="off"></label></div><div class="card-actions">${local ? '<button class="primary-button" data-settings-action="open-cloud-folder">Abrir pasta</button>' : ""}<button class="secondary-button" data-settings-action="copy-cloud-path">Copiar caminho</button>${cloud.configured ? '<button class="primary-button" data-settings-action="save-cloud-folders-settings">Salvar pastas</button>' : ""}${local && cloud.configured ? '<button class="secondary-button" data-settings-action="consolidate-cloud-projects">Copiar projetos externos</button>' : ""}</div></article>
        <article class="maintenance-card"><h3>Última sincronização</h3><p>${cloud.status?.finished_at ? escapeHTML(formatTime(cloud.status.finished_at)) : "Nenhuma execução registrada"}</p><span class="value">${cloud.status?.running ? "Em andamento" : cloud.status?.ok ? "Concluída com sucesso" : cloud.status?.error ? `Falhou: ${escapeHTML(String(cloud.status.error).slice(0, 180))}` : "Aguardando"}</span></article>
        <article class="maintenance-card"><h3>Política automática</h3><p>Conflitos são preservados e exclusões em massa interrompem a execução.</p><div class="cloud-form"><label class="field-label">Intervalo<select id="settings-cloud-interval" ${!cloud.initialized ? "disabled" : ""}><option value="5" ${Number(cloud.interval_minutes) === 5 ? "selected" : ""}>5 minutos</option><option value="15" ${Number(cloud.interval_minutes) === 15 ? "selected" : ""}>15 minutos</option><option value="30" ${Number(cloud.interval_minutes) === 30 ? "selected" : ""}>30 minutos</option><option value="60" ${Number(cloud.interval_minutes) === 60 ? "selected" : ""}>1 hora</option></select></label><label class="field-label">Conteúdo<select id="settings-cloud-filter" ${!cloud.configured ? "disabled" : ""}><option value="source" ${cloud.filter_profile !== "complete" ? "selected" : ""}>Código seguro</option><option value="complete" ${cloud.filter_profile === "complete" ? "selected" : ""}>Projeto completo</option></select></label></div></article>
      </div>
    </section>

    <section id="security-control" class="settings-section">
      <div class="settings-section-title"><div><h3>Acesso externo de alta segurança</h3><p>Cloudflare Access ou Tailscale, identidade exata, chave do dispositivo e nenhuma porta administrativa pública.</p></div></div>
      <div class="maintenance-grid">
        <article class="maintenance-card"><h3>${cloudflareIdentity ? "Cloudflare Access ativo" : tailscale.installed ? "Tailscale instalado" : "Rede privada não configurada"}</h3><p>${cloudflareIdentity ? "Identidade Cloudflare verificada" : escapeHTML(tailscale.version || "Acesso somente local")}</p><span class="value">${cloudflareIdentity ? escapeHTML(state.identity) : tailscale.connected ? `Conectado: ${escapeHTML(tailscale.login || tailscale.dns_name || "sim")}` : "Desconectado"}</span>${local ? `<div class="card-actions">${!tailscale.installed ? '<button class="secondary-button" data-settings-action="install-tailscale">Instalar Tailscale opcional</button>' : ""}${tailscale.installed && !tailscale.connected ? '<button class="secondary-button" data-settings-action="connect-tailscale">Conectar Tailscale</button>' : ""}</div>` : ""}</article>
        <article class="maintenance-card"><h3>${security.remote_enabled ? "HTTPS privado ativo" : "Acesso remoto desativado"}</h3><p>${escapeHTML(security.external_url || "Somente localhost")}</p><span class="value">${escapeHTML(security.allowed_tailscale_login || "Nenhuma identidade externa liberada")}</span>${local ? `<div class="card-actions">${tailscale.connected && !security.remote_enabled ? '<button class="primary-button" data-settings-action="enable-remote">Ativar</button>' : ""}${security.remote_enabled ? '<button class="danger-button" data-settings-action="disable-remote">Desativar</button>' : ""}</div>` : ""}</article>
        <article class="maintenance-card"><h3>Dispositivos cadastrados</h3><p>${security.paired_devices || devices.length}/${deviceLimit} dispositivo(s) autorizado(s)</p><span class="value">Novos dispositivos podem ser aprovados ou recusados por uma sessão já pareada</span><div class="card-actions"><button class="primary-button" data-settings-action="pair-device" ${!security.remote_enabled ? "disabled" : ""}>Pareamento manual</button></div></article>
        <article class="maintenance-card"><h3>Sessão atual</h3><p>${state.session?.device_id ? "Chave do dispositivo verificada" : "Acesso local confiável"}</p><span class="value">${escapeHTML(state.session?.device_id || "localhost")}</span></article>
      </div>
      ${deviceAdmin ? `<div class="settings-project-list device-list">${deviceRows}</div><h4>Solicitações pendentes</h4><div class="settings-project-list device-list">${enrollmentRows}</div>` : '<div class="inline-notice">A administração de dispositivos exige uma sessão já pareada.</div>'}
    </section>

    <section id="diagnostics-control" class="settings-section">
      <div class="settings-section-title"><div><h3>Atualização e diagnóstico</h3><p>Manutenção completa por botões.</p></div></div>
      <div class="maintenance-grid">
        <article class="maintenance-card switch-row"><div><h3>Atualizações automáticas do Dex</h3><p>Aplica releases pendentes somente depois que todas as conversas terminarem. Ativado por padrão.</p></div><label class="switch"><input id="settings-system-update-automatic" type="checkbox" ${state.systemUpdateAutomatic ? "checked" : ""}><span></span></label></article>
        <article class="maintenance-card"><h3>Atualizar o aplicativo</h3><p>Selecione um novo pacote .deb em uma janela do Linux.</p><div class="card-actions"><button class="primary-button" data-settings-action="update-app" ${!local ? "disabled" : ""}>Escolher arquivo .deb</button></div></article>
        <article class="maintenance-card"><h3>Relatório de diagnóstico</h3><p>Baixa estado e registros recentes.</p><div class="card-actions"><button class="secondary-button" data-settings-action="download-diagnostics">Baixar relatório</button><button class="secondary-button" data-settings-action="load-logs">Ver registros</button></div></article>
        <article class="maintenance-card full"><h3>Registros do aplicativo</h3><p>Os detalhes aparecem aqui sem usar terminal.</p><pre id="settings-logs" class="logs-box">Selecione “Ver registros”.</pre></article>
      </div>
    </section>`;
  const categoryDefinitions = {
    "sites-control": {title:"Sites", description:"Publicação e origens locais de sasocq.com"},
    "steam-control": {title:"Steam e jogos", description:"HDMI, biblioteca, emuladores e armazenamento"},
    "backup-recovery": {title:"Backup e recuperação", description:"Backups, watchdog e restauração segura"},
  };
  const cardGroups = {
    "Publicação dos sites sasocq.com":"sites-control",
    "Steam Machine / HDMI":"steam-control",
    "Disco externo de jogos":"steam-control",
    "Emuladores e biblioteca universal":"steam-control",
    "Próxima sessão no HDMI":"steam-control",
    "Backup e recuperação":"backup-recovery",
    "Autorrecuperação do host":"backup-recovery",
  };
  Object.entries(categoryDefinitions).forEach(([id, definition]) => {
    if (document.getElementById(id)) return;
    const section = document.createElement("section");
    section.id = id;
    section.className = "settings-section";
    section.innerHTML = `<div class="settings-section-title"><div><h3>${escapeHTML(definition.title)}</h3><p>${escapeHTML(definition.description)}</p></div></div><div class="maintenance-grid"></div>`;
    selectors.settingsContent.appendChild(section);
  });
  selectors.settingsContent.querySelectorAll(".maintenance-card > h3").forEach(heading => {
    const group = cardGroups[heading.textContent.trim()];
    if (group) document.querySelector(`#${group} .maintenance-grid`)?.appendChild(heading.parentElement);
  });
  const mergedSections = {
    "application-control":"diagnostics-control",
    "tools-control":"codex-account",
    "cloud-projects-control":"projects-control",
  };
  Object.entries(mergedSections).forEach(([sourceId, targetId]) => {
    const source = document.getElementById(sourceId);
    const target = document.getElementById(targetId);
    if (!source || !target) return;
    Array.from(source.children).forEach(child => target.appendChild(child));
    source.remove();
  });
  if (!backupCloud.configured) {
    const pathInput = document.getElementById("backup-cloud-remote-path");
    const backupCard = pathInput?.closest(".maintenance-card");
    const actions = backupCard?.querySelector(".card-actions");
    if (actions && !actions.querySelector('[data-settings-action="backup-cloud-browse"]')) {
      const browseButton = document.createElement("button");
      browseButton.className = "primary-button";
      browseButton.dataset.settingsAction = "backup-cloud-browse";
      browseButton.textContent = "Escolher pasta no OneDrive";
      browseButton.disabled = true;
      browseButton.title = "Conclua primeiro a conexão do OneDrive no servidor";
      actions.prepend(browseButton);
    }
    if (backupCard && backupCloud.session?.error) {
      const warning = document.createElement("div");
      warning.className = "inline-notice error";
      warning.textContent = "O login anterior foi interrompido no servidor. Selecione Conectar OneDrive novamente e conclua a autorização; depois o seletor de pastas será liberado.";
      actions?.before(warning);
    }
  }
  const projectCloudInput = document.getElementById("settings-cloud-remote-path");
  const projectCloudActions = projectCloudInput?.closest(".maintenance-card")?.querySelector(".card-actions");
  if (projectCloudActions && !projectCloudActions.querySelector('[data-settings-action="browse-cloud-project-folder"]')) {
    const browseProjects = document.createElement("button");
    browseProjects.className = "primary-button";
    browseProjects.dataset.settingsAction = "browse-cloud-project-folder";
    browseProjects.textContent = "Escolher pasta no OneDrive";
    browseProjects.disabled = !cloud.configured;
    browseProjects.title = cloud.configured ? "Navegar pelas pastas dos projetos" : "Conecte primeiro o OneDrive dos projetos";
    projectCloudActions.prepend(browseProjects);
  }
  renderBackupHistory(control.backup?.history || []);
  const backupRunButton = selectors.settingsContent.querySelector('[data-settings-action="backup-run"]');
  if (backupRunButton && control.backup?.status === "running") backupRunButton.textContent = "Acompanhar backup em andamento";
  showSettingsPage(state.settingsPage);
}

function showSettingsPage(pageId = "overview") {
  const page = pageId && document.getElementById(pageId) ? pageId : "overview";
  state.settingsPage = page;
  const navigation = selectors.settingsContent?.querySelector(".settings-category-nav");
  const header = selectors.settingsContent?.querySelector(".settings-subpage-header");
  const title = selectors.settingsContent?.querySelector("#settings-subpage-title");
  const selectedButton = navigation?.querySelector(`[data-settings-jump="${CSS.escape(page)}"]`);
  navigation?.classList.toggle("hidden", page !== "overview");
  header?.classList.toggle("hidden", page === "overview");
  if (title) title.textContent = selectedButton?.dataset.settingsTitle || "Configurações";
  selectors.settingsContent?.querySelectorAll(".settings-section").forEach(section => {
    section.classList.toggle("hidden", page === "overview" || section.id !== page);
  });
  if (selectors.settingsContent) selectors.settingsContent.scrollTop = 0;
}

async function sendControl(action, params = {}, destructive = false) {
  const response = await api("/api/control/action", {
    method:"POST",
    body:JSON.stringify({action, params, confirmation:destructive ? "CONFIRMAR" : ""}),
  });
  await loadStatus();
  toast("Operação administrativa concluída.", "success");
  return response;
}

function controlOutput(response) {
  return response?.result?.output ?? response?.result ?? response ?? {};
}

function chooseIndexed(title, values, formatter) {
  if (!values?.length) return null;
  const text = values.map((item, index) => `${index + 1}. ${formatter(item, index)}`).join("\n");
  const answer = window.prompt(`${title}\n\n${text}\n\nDigite o número:`, "1");
  if (answer === null) return null;
  const index = Number(answer) - 1;
  if (!Number.isInteger(index) || index < 0 || index >= values.length) throw new Error("Seleção inválida");
  return values[index];
}

async function restoreBackupFlow() {
  const snapshotsResponse = await sendControl("backup", {operation:"snapshots"});
  const snapshots = controlOutput(snapshotsResponse)?.snapshots || [];
  const selected = chooseIndexed("Escolha o snapshot do servidor", snapshots, item => `${item.id} • ${formatTime(item.time)} • ${(item.tags || []).join(", ") || "sem tag"}`);
  if (!selected) return;
  const snapshotId = selected.full_id || selected.id;
  await sendControl("backup", {operation:"validate-restore", snapshot_id:snapshotId}, true);
  if (!window.confirm(`Snapshot ${selected.id} validado. Restaurar agora a VM sasocq-server? A VM será parada e haverá rollback automático se a validação pós-cópia falhar.`)) return;
  return sendControl("backup", {operation:"restore", snapshot_id:snapshotId}, true);
}

async function configureWorkerLimits(projectId) {
  const worker = state.lastStatus?.control?.workers?.projects?.[projectId] || {};
  const current = worker.limits || {};
  const cpu = window.prompt("Limite de CPU deste projeto em %. Use 0 para o perfil dinâmico; 100 equivale a um núcleo lógico:", String(current.cpu_quota_percent || 0));
  if (cpu === null) return;
  const memoryHigh = window.prompt("Faixa de pressão de memória (MemoryHigh) em MiB. Use 0 para automático:", String(current.memory_high_mib || 0));
  if (memoryHigh === null) return;
  const memoryMax = window.prompt("Limite rígido de memória (MemoryMax) em MiB. Use 0 para ilimitado; não pode ser menor que MemoryHigh:", String(current.memory_max_mib || 0));
  if (memoryMax === null) return;
  const ioWeight = window.prompt("Peso de I/O de 1 a 10000. Use 0 para seguir a prioridade:", String(current.io_weight || 0));
  if (ioWeight === null) return;
  const values = [cpu, memoryHigh, memoryMax, ioWeight].map(value => Number(value));
  if (values.some(value => !Number.isFinite(value) || value < 0)) return toast("Os limites precisam ser números não negativos.", "error");
  return sendControl("workers", {
    operation:"limits",
    project_id:projectId,
    cpu_quota_percent:values[0],
    memory_high_mib:values[1],
    memory_max_mib:values[2],
    io_weight:values[3],
  });
}

async function handleSettingsAction(action) {
  if (action === "authd-prepare") return sendControl("authd", {operation:"prepare"});
  if (action === "authd-configure") {
    const tenant_id = el("authd-tenant-id")?.value.trim() || "";
    const client_id = el("authd-client-id")?.value.trim() || "";
    const owner_email = el("authd-owner-email")?.value.trim() || "";
    const register_device = Boolean(el("authd-register-device")?.checked);
    if (!tenant_id || !client_id || !owner_email) return toast("Preencha Tenant ID, Client ID e e-mail proprietário.", "error");
    if (!window.confirm(`Vincular o login gráfico do Ubuntu a ${owner_email}? A conta continuará sem sudo e independente do OneDrive.`)) return;
    return sendControl("authd", {operation:"configure", tenant_id, client_id, owner_email, register_device, force_online_check:true}, true);
  }
  if (action === "publication-install") return sendControl("publication", {operation:"install"});
  if (action === "publication-configure") {
    const token = el("publication-token")?.value.trim() || "";
    const hostnames = (el("publication-hostnames")?.value || "sasocq.com").split(/[\s,;]+/).filter(Boolean);
    if (!token) return toast("Cole o token do Cloudflare Tunnel criado para sasocq.com.", "error");
    if (!window.confirm("Ativar o túnel público no servidor? O token será enviado somente à VM e não será registrado no histórico.")) return;
    const result = await sendControl("publication", {operation:"configure", token, hostnames}, true);
    if (el("publication-token")) el("publication-token").value = "";
    return result;
  }
  if (action === "packages-install" || action === "packages-remove") {
    const raw = el("system-packages")?.value || "";
    const packages = raw.split(/[\s,;]+/).map(item => item.trim()).filter(Boolean);
    if (!packages.length) return toast("Informe ao menos um pacote Ubuntu.", "error");
    const remove = action === "packages-remove";
    if (remove && !window.confirm(`Remover ${packages.join(", ")}? Dependências podem ser afetadas.`)) return;
    return sendControl("packages", {operation:remove ? "remove" : "install", packages}, true);
  }
  if (action.startsWith("worker-limits:")) return configureWorkerLimits(action.slice(action.indexOf(":") + 1));
  if (action.startsWith("worker-pause:")) return sendControl("workers", {operation:"pause", project_id:action.slice(action.indexOf(":") + 1)});
  if (action.startsWith("worker-resume:")) return sendControl("workers", {operation:"resume", project_id:action.slice(action.indexOf(":") + 1)});
  if (action.startsWith("worker-stop:")) {
    const projectId = action.slice(action.indexOf(":") + 1);
    if (!window.confirm("Encerrar somente este worker de projeto? A conversa continuará salva e poderá ser retomada.")) return;
    return sendControl("workers", {operation:"stop", project_id:projectId}, true);
  }
  if (action.startsWith("resources-")) return sendControl("resources", {operation:"set-mode", mode:action.replace("resources-", "")});
  if (action === "vm-reconcile") { if (!window.confirm("Aplicar à configuração persistente da VM a recomendação atual de CPU e memória? A mudança valerá no próximo reboot da VM.")) return; return sendControl("vm", {operation:"reconcile-resources", name:"sasocq-server"}, true); }
  if (action === "steam-start") return sendControl("steam", {operation:"start"});
  if (action === "steam-sync-library") return sendControl("steam", {operation:"sync-library"});
  if (action === "steam-open-heroic") return sendControl("gaming", {operation:"open-store", store:"heroic"});
  if (action === "steam-open-lutris") return sendControl("gaming", {operation:"open-store", store:"lutris"});
  if (action === "steam-open-bottles") return sendControl("gaming", {operation:"open-store", store:"bottles"});
  if (action === "steam-stop") { if (!window.confirm("Fechar o Steam e o jogo em execução?")) return; return sendControl("steam", {operation:"stop"}, true); }
  if (action === "game-storage-ensure") return sendControl("game-storage", {operation:"ensure"});
  if (action === "game-storage-adopt") {
    const storage = state.lastStatus?.control?.game_storage || {};
    const partitions = (storage.candidates || []).flatMap(disk => (disk.partitions || []).filter(part => part.uuid).map(part => ({...part, disk})));
    const selected = chooseIndexed("Escolha o volume externo existente", partitions, item => `${item.path} • ${item.fstype || "formato desconhecido"} • ${formatBytes(item.size)} • ${item.disk.model || item.disk.path}`);
    if (!selected) return;
    if (!window.confirm(`Usar ${selected.path} como biblioteca permanente de jogos? Arquivos existentes serão preservados, mas pastas SASOCQ serão criadas.`)) return;
    return sendControl("game-storage", {operation:"adopt", uuid:selected.uuid}, true);
  }
  if (action === "game-storage-prepare") {
    const storage = state.lastStatus?.control?.game_storage || {};
    const selected = chooseIndexed("ESCOLHA O DISCO EXTERNO QUE SERÁ APAGADO", storage.candidates || [], item => `${item.path} • ${item.model || "disco externo"} • ${formatBytes(item.size)} • serial ${item.serial || "não informado"}`);
    if (!selected) return;
    const phrase = window.prompt(`Todos os dados de ${selected.path} serão apagados. Digite exatamente APAGAR DISCO EXTERNO para continuar:`);
    if (phrase !== "APAGAR DISCO EXTERNO") return toast("Formatação cancelada.", "error");
    return sendControl("game-storage", {operation:"prepare", device:selected.path}, true);
  }
  if (action === "emulation-install") {
    await sendControl("emulation", {operation:"install"});
    await sendControl("emulation", {operation:"scan"});
    return sendControl("steam", {operation:"sync-library"});
  }
  if (action === "emulation-scan") {
    await sendControl("emulation", {operation:"scan"});
    return sendControl("steam", {operation:"sync-library"});
  }
  if (action === "watchdog-check") return sendControl("watchdog", {operation:"check"});
  if (action === "watchdog-install") {
    if (!window.confirm("Reaplicar a proteção de watchdog, panic/lockup, initramfs e serviços de autorrecuperação?")) return;
    return sendControl("watchdog", {operation:"install", daemon_reexec:true}, true);
  }
  if (action === "watchdog-clear-quarantine") {
    if (!window.confirm("Liberar os workers de projetos que permaneceram pausados após a recuperação crítica?")) return;
    return sendControl("watchdog", {operation:"clear-quarantine", resume_workers:true}, true);
  }
  if (action === "watchdog-reboot-test") {
    const phrase = window.prompt("O mini PC será reiniciado e deverá restaurar servidor e Codex automaticamente. Digite TESTAR REINÍCIO:");
    if (phrase !== "TESTAR REINÍCIO") return toast("Teste cancelado.", "error");
    return sendControl("watchdog", {operation:"reboot-test", reason:"teste explícito pelo painel remoto"}, true);
  }
  if (action === "power-policy-install") return sendControl("system", {operation:"install-power-policy"});
  if (action === "remote-physical-desktop") { selectors.settingsDialog.close(); return openRemoteDesktop({target:"desktop"}); }
  if (action === "remote-physical-games") { selectors.settingsDialog.close(); return openRemoteDesktop({target:"jogos"}); }
  if (action === "workers-pause") return sendControl("workers", {operation:"pause"});
  if (action === "workers-resume") return sendControl("workers", {operation:"resume"});
  if (action === "workers-stop") { if (!window.confirm("Encerrar todos os workers de projetos em execução?")) return; return sendControl("workers", {operation:"terminate", confirm:true}, true); }
  if (action === "backup-cloud-install") return startBackgroundTask("/api/backup-cloud/install");
  if (action === "backup-cloud-browse") return openOneDriveFolderBrowser("backup");
  if (action === "backup-cloud-connect") return startBackgroundTask("/api/backup-cloud/config/start", {provider:"onedrive", client_id:"", client_secret:"", remote_path:backupRemotePath()});
  if (action === "backup-cloud-answer") {
    const session = state.lastStatus?.backup_cloud?.session;
    const answer = el("backup-cloud-question-answer")?.value ?? "";
    if (!session?.id) return toast("A sessão da conta OneDrive de backup expirou. Conecte novamente.", "error");
    return startBackgroundTask("/api/backup-cloud/config/answer", {session_id:session.id, answer, remote_path:backupRemotePath()});
  }
  if (action === "backup-cloud-activate") {
    const remotePath = backupRemotePath();
    await api("/api/backup-cloud/activate", {method:"POST", body:JSON.stringify({remote_path:remotePath})});
    await loadStatus(); toast(`Pasta de backup salva: ${remotePath}`, "success"); return;
  }
  if (action === "backup-run") return startBackgroundTask("/api/backup/run");
  if (action === "backup-restore") return restoreBackupFlow();
  if (action === "recovery-prepare") return sendControl("recovery", {operation:"prepare"});
  if (action === "recovery-repair") { if (!window.confirm("Executar reparo de pacotes, initramfs e GRUB sem formatar o PC?")) return; return sendControl("recovery", {operation:"repair"}, true); }
  if (action === "recovery-reapply") { if (!window.confirm("Reaplicar toda a receita SASOCQ sem apagar dados?")) return; return sendControl("recovery", {operation:"reapply"}, true); }
  if (action === "recovery-factory-reset") {
    const phrase = window.prompt("O Ubuntu do SSD interno será reinstalado pelo ambiente local. Jogos do disco externo serão preservados. Digite RESTAURAR ESTE PC:");
    if (phrase !== "RESTAURAR ESTE PC") return toast("Restauração cancelada.", "error");
    await sendControl("recovery", {operation:"factory-reset"}, true);
    if (!window.confirm("Restauração agendada. Reiniciar agora para iniciar o processo?")) return;
    return sendControl("system", {operation:"reboot"}, true);
  }
  if (action === "recovery-export") {
    if (!window.confirm("Baixar agora as credenciais locais de recuperação? O arquivo contém segredos administrativos e deve ser guardado criptografado fora do mini PC.")) return;
    const response = await sendControl("recovery", {operation:"export"}, true);
    const bundle = response?.result?.output || response?.result || {};
    const blob = new Blob([JSON.stringify(bundle, null, 2) + "\n"], {type:"application/json"});
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `sasocq-recuperacao-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return;
  }
  if (action === "provision-run") return sendControl("provision.run", {});
  if (action === "session-games") return sendControl("session", {operation:"switch-next-boot", user:"jogos"});
  if (action === "session-desktop") return sendControl("session", {operation:"switch-next-boot", user:"desktop"});
  if (action === "session-codex") return sendControl("session", {operation:"switch-next-boot", user:"codex"});
  if (action === "system-update") { if (!window.confirm("Instalar agora todas as atualizações disponíveis do Ubuntu?")) return; return sendControl("system", {operation:"update"}); }
  if (action === "system-reboot") { if (!window.confirm("Reiniciar o mini PC agora? Sites, projetos e jogos serão interrompidos brevemente.")) return; return sendControl("system", {operation:"reboot"}, true); }
  if (action === "restart-app") {
    if (!window.confirm("Reiniciar o Codex Linux Control agora?")) return;
    await api("/api/system/restart", {method:"POST"});
    selectors.settingsDialog.close();
    toast("O aplicativo está reiniciando.");
    setTimeout(() => location.reload(), 3500);
    return;
  }
  if (action === "update-codex") return startBackgroundTask("/api/system/codex/update");
  if (action === "refresh-codex-usage") {
    await loadRateLimits("system", {announce:true});
    return;
  }
  if (action === "check-codex-upstream") {
    toast("Verificando versão, schema e capacidades do Codex…");
    const result = await api("/api/upstream/check", {method:"POST"});
    await loadStatus();
    toast(result.status === "ok" ? "Registro upstream atualizado." : "Registro atualizado com avisos.", result.status === "ok" ? "success" : "warning");
    return;
  }
  if (action === "open-chatgpt-desktop") {
    selectors.settingsDialog.close();
    return openRemoteDesktop();
  }
  if (action === "install-full-experience") return startBackgroundTask("/api/setup/full-experience/install");
  if (action === "open-tools") { selectors.settingsDialog.close(); return openToolsDialog(); }
  if (action === "login-codex-system") return startDeviceLogin("system");
  if (action === "share-codex-account") {
    return shareCodexAccount();
  }
  if (action === "logout-codex-system") {
    if (!window.confirm("Sair da conta única do Codex no Sistema e nos Projetos?")) return;
    await api(`/api/account/logout?${workspaceQuery("system")}`, {method:"POST"});
    await loadAccount({}, "system");
    await loadAccount({}, "projects");
    await loadStatus();
    return;
  }
  if (action === "add-project") return addProjectGraphically(false);
  if (action === "configure-cloud") {
    selectors.settingsDialog.close();
    state.setup.active = true;
    state.setup.step = 4;
    state.setup.cloudChoiceTouched = false;
    selectors.setupOverlay.classList.remove("hidden");
    await refreshSetupState();
    return;
  }
  if (action === "sync-cloud-now") return startBackgroundTask("/api/cloud/sync/now");
  if (action === "save-cloud-folders-settings") {
    const local_path = el("settings-cloud-local-path")?.value.trim() || state.lastStatus?.cloud_sync?.local_path;
    const remote_path = el("settings-cloud-remote-path")?.value.trim() || "Codex Linux Control/Projetos";
    await api("/api/cloud/folder", {method:"POST", body:JSON.stringify({local_path, remote_path})});
    await loadStatus(); toast("Pastas dos projetos salvas.", "success"); return;
  }
  if (action === "browse-cloud-project-folder") return openOneDriveFolderBrowser("projects");
  if (action === "open-cloud-folder") { await api("/api/cloud/open-folder", {method:"POST"}); return; }
  if (action === "copy-cloud-path") {
    const value = state.lastStatus?.cloud_sync?.local_path || "";
    try { await navigator.clipboard.writeText(value); toast("Caminho copiado.", "success"); }
    catch { window.prompt("Copie o caminho:", value); }
    return;
  }
  if (action === "consolidate-cloud-projects") return startBackgroundTask("/api/cloud/projects/consolidate");
  if (action === "pause-cloud") { await api("/api/cloud/disable", {method:"POST"}); await loadStatus(); toast("Sincronização automática pausada.", "success"); return; }
  if (action === "resume-cloud") {
    const interval = Number(state.lastStatus?.cloud_sync?.interval_minutes || 5);
    await api("/api/cloud/timer", {method:"POST", body:JSON.stringify({enabled:true, interval_minutes:interval})});
    await loadStatus(); toast("Sincronização automática retomada.", "success"); return;
  }
  if (action === "prepare-remote") return startBackgroundTask("/api/setup/remote/prepare");
  if (action === "install-tailscale") return startBackgroundTask("/api/setup/tailscale/install");
  if (action === "connect-tailscale") return startBackgroundTask("/api/setup/tailscale/connect");
  if (action === "enable-remote") return startBackgroundTask("/api/setup/tailscale/serve");
  if (action === "disable-remote") return startBackgroundTask("/api/setup/tailscale/disable");
  if (action === "pair-device") return showPairingDialog();
  if (action === "open-remote-desktop") { selectors.settingsDialog.close(); return openRemoteEnvironmentChooser(); }
  if (action === "open-remote-browser") { selectors.settingsDialog.close(); return openRemoteDesktop({browser:true}); }
  if (action === "stop-remote-desktop") return stopRemoteDesktop();
  if (action === "update-app") return startBackgroundTask("/api/system/update/select-deb");
  if (action === "download-diagnostics") {
    const anchor = document.createElement("a");
    anchor.href = "/api/system/diagnostics";
    anchor.download = "codex-linux-control-diagnostico.json";
    anchor.click();
    return;
  }
  if (action === "load-logs") {
    const data = await api("/api/system/logs?lines=300");
    const box = el("settings-logs");
    if (box) { box.textContent = data.logs || "Nenhum registro."; box.scrollTop = box.scrollHeight; }
  }
}

async function startDeviceLogin(workspace = activeWorkspace()) {
  try {
    if (selectors.settingsDialog.open) selectors.settingsDialog.close();
    state.loginWorkspace = workspace;
    const data = await api(`/api/account/login/device-code?${workspaceQuery(workspace)}`, {method:"POST"});
    el("verification-link").href = data.verificationUrl;
    el("user-code").textContent = data.userCode;
    el("login-status").textContent = `Aguardando autenticação em ${workspaceLabel(workspace)}…`;
    const title = selectors.loginDialog.querySelector("h2");
    if (title) title.textContent = `Entrar com ChatGPT — ${workspaceLabel(workspace)}`;
    if (!selectors.loginDialog.open) selectors.loginDialog.showModal();
  } catch (error) { toast(error.message, "error"); }
}

async function shareCodexAccount({silent = false} = {}) {
  await api("/api/account/share-projects", {method:"POST"});
  await loadAccount({}, "system");
  await loadAccount({}, "projects");
  await loadStatus();
  if (!silent) toast("A mesma conta foi aplicada ao Codex de Projetos.", "success");
}

async function renameActiveThread() {
  if (!state.activeThreadId) return;
  const name = window.prompt("Novo nome da conversa:", selectors.title.textContent);
  if (!name?.trim()) return;
  try {
    await api(`/api/threads/${encodeURIComponent(state.activeThreadId)}/name`, {method:"PATCH", body:JSON.stringify({name:name.trim()})});
    selectors.title.textContent = name.trim();
    loadThreads();
  } catch (error) { toast(error.message, "error"); }
}

async function archiveActiveThread() {
  if (!state.activeThreadId || !window.confirm("Arquivar esta conversa?")) return;
  try {
    await api(`/api/threads/${encodeURIComponent(state.activeThreadId)}/archive`, {method:"POST"});
    newThread(); loadThreads();
  } catch (error) { toast(error.message, "error"); }
}

async function openToolsDialog() {
  if (!state.activeProject) return toast("Selecione um projeto primeiro.", "error");
  state.toolsScope = "thread";
  state.toolsTab = "recommended";
  document.querySelectorAll('input[name="tool-scope"]').forEach(input => {
    input.checked = input.value === state.toolsScope;
  });
  if (!selectors.toolsDialog.open) selectors.toolsDialog.showModal();
  await loadExtensions(false);
}

async function loadExtensions(refresh = false) {
  if (!state.activeProject) return;
  state.extensionsLoading = true;
  state.extensionsError = "";
  if (state.extensions) renderToolsDialog();
  else selectors.toolsContent.innerHTML = '<div class="panel-empty">Carregando skills, apps e servidores MCP…</div>';
  try {
    const query = new URLSearchParams({project_id:state.activeProject.id, refresh:String(refresh)});
    if (state.activeThreadId) query.set("thread_id", state.activeThreadId);
    state.extensions = await api(`/api/extensions?${query}`);
    state.toolProfile = normalizeToolProfile(state.extensions.profile || state.toolProfile);
    renderToolProfileChip();
  } catch (error) {
    state.extensionsError = error.message;
  } finally {
    state.extensionsLoading = false;
    if (state.extensions) renderToolsDialog();
    else selectors.toolsContent.innerHTML = `<div class="inline-notice">Não foi possível carregar o catálogo agora. ${escapeHTML(state.extensionsError)}</div>`;
  }
}

function extensionName(item, fallback = "Extensão") {
  return item.interface?.displayName || item.interface?.display_name || item.displayName || item.display_name || item.name || item.title || item.id || fallback;
}

function extensionDescription(item) {
  return item.interface?.shortDescription || item.interface?.short_description || item.shortDescription || item.short_description || item.description || item.summary || "";
}

function itemSelected(kind, item) {
  if (kind === "skill") return state.toolProfile.skills.some(value => value.path === item.path);
  if (kind === "app") return state.toolProfile.apps.some(value => value.id === (item.id || item.appId));
  if (kind === "mcp") return state.toolProfile.mcp_servers.includes(item.name || item.id || item.serverName);
  return false;
}

function boolish(value, fallback = true) {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") return !["disabled", "unavailable", "false", "off"].includes(value.toLowerCase());
  return fallback;
}

function mcpName(item) { return item.name || item.id || item.serverName || item.server || "MCP"; }
function mcpEnabled(item) { return boolish(item.enabled ?? item.isEnabled ?? item.status?.enabled, true); }
function appEnabled(item) { return boolish(item.enabled ?? item.isEnabled ?? item.accessibility?.enabled, true); }
function skillEnabled(item) { return boolish(item.enabled, true); }

function renderToolsDialog() {
  const data = state.extensions || {};
  document.querySelectorAll(".tools-tab").forEach(button => button.classList.toggle("active", button.dataset.toolsTab === state.toolsTab));
  document.querySelectorAll('input[name="tool-scope"]').forEach(input => input.checked = input.value === state.toolsScope);
  if (state.toolsTab === "recommended") renderRecommendedTools(data);
  else if (state.toolsTab === "skills") renderSkillTools(data);
  else if (state.toolsTab === "apps") renderAppTools(data);
  else if (state.toolsTab === "plugins") renderPluginTools(data);
  else renderMcpTools(data);
  const count = toolProfileCount();
  selectors.toolsSummary.textContent = count ? `${count} recurso${count === 1 ? "" : "s"} associado${count === 1 ? "" : "s"} • seleção automática ativa` : "Seleção automática de ferramentas e plugins ativa";
}

function renderRecommendedTools(data) {
  const full = data.full_experience || {};
  const catalogSummary = (key, errorKey, singular, plural) => {
    const count = (data[key] || []).length;
    if (!count && data.errors?.[errorKey]) return `${plural}: consulta temporariamente indisponível.`;
    return `${count} ${count === 1 ? singular : plural} detectado${count === 1 ? "" : "s"}.`;
  };
  const errorSources = Object.entries(data.errors || {}).filter(([, value]) => Boolean(value)).map(([key]) => key);
  const catalogNotice = state.extensionsLoading
    ? '<div class="inline-notice">Atualizando o catálogo sem ocultar as opções já carregadas…</div>'
    : state.extensionsError
      ? `<div class="inline-notice">A atualização falhou; as últimas opções válidas foram preservadas. ${escapeHTML(state.extensionsError)}</div>`
      : errorSources.length
        ? `<div class="inline-notice">Algumas fontes estão temporariamente degradadas (${escapeHTML(errorSources.join(", "))}); as opções locais e o último catálogo válido foram preservados.</div>`
        : "";
  selectors.toolsContent.innerHTML = `<div class="tool-grid">
    ${data.project_kind === "system" ? '<article class="tool-card featured"><div class="tool-card-head"><span class="tool-icon">◇</span><div><strong>Administração SASOCQ</strong><small>Host, KVM, recursos, backups, sessões, Steam e serviços pelo Control Plane auditável.</small></div><input aria-label="Administração SASOCQ associada permanentemente" type="checkbox" checked disabled></div><div class="tool-card-foot"><span class="status-chip ok">Associada automaticamente</span></div></article>' : ""}
    <article class="tool-card featured"><div class="tool-card-head"><span class="tool-icon">◎</span><div><strong>Navegador Playwright</strong><small>Chromium visível, perfil persistente, DOM, console, rede, arquivos e capturas.</small></div><input aria-label="Associar navegador à conversa" type="checkbox" data-profile-toggle="browser" ${state.toolProfile.browser ? "checked" : ""} ${!full.playwright?.installed ? "disabled" : ""}></div><div class="tool-card-foot"><span class="status-chip ${full.playwright?.enabled ? "ok" : "warn"}">${full.playwright?.installed ? (full.playwright?.enabled ? "MCP ativo" : "MCP desativado") : "Não instalado"}</span>${full.playwright?.installed ? '<button class="ghost-button" data-extension-toggle="mcp" data-extension-name="playwright" data-extension-enabled="'+(!full.playwright?.enabled)+'">'+(full.playwright?.enabled ? "Desativar globalmente" : "Ativar globalmente")+'</button>' : ""}</div></article>
    <article class="tool-card featured"><div class="tool-card-head"><span class="tool-icon">▣</span><div><strong>Desktop Linux supervisionado</strong><small>Tela, acessibilidade, janelas, mouse, teclado e área de transferência com aprovações.</small></div><input aria-label="Associar controle do desktop à conversa" type="checkbox" data-profile-toggle="desktop" ${state.toolProfile.desktop ? "checked" : ""} ${!full.installed ? "disabled" : ""}></div><div class="tool-card-foot"><span class="status-chip ${full.desktop?.enabled ? "ok" : "warn"}">${full.desktop?.session_type || "Linux"} • ${full.desktop?.input_backend || "entrada indisponível"}</span>${full.installed ? '<button class="ghost-button" data-extension-toggle="mcp" data-extension-name="linux_desktop" data-extension-enabled="'+(!full.desktop?.enabled)+'">'+(full.desktop?.enabled ? "Desativar globalmente" : "Ativar globalmente")+'</button>' : ""}</div></article>
    <article class="tool-card"><div class="tool-card-head"><span class="tool-icon">✦</span><div><strong>Skills</strong><small>${catalogSummary("skills", "skills", "instrução especializada", "instruções especializadas")}</small></div></div><button class="secondary-button" data-open-tools-tab="skills">Escolher skills</button></article>
    <article class="tool-card"><div class="tool-card-head"><span class="tool-icon">⌘</span><div><strong>Apps e conectores</strong><small>${catalogSummary("apps", "apps", "app disponível", "apps disponíveis")}</small></div></div><button class="secondary-button" data-open-tools-tab="apps">Escolher apps</button></article>
    <article class="tool-card"><div class="tool-card-head"><span class="tool-icon">⬡</span><div><strong>Catálogo de plugins</strong><small>${catalogSummary("plugins", "plugins", "plugin", "plugins")}</small></div></div><button class="secondary-button" data-open-tools-tab="plugins">Abrir catálogo</button></article>
    <article class="tool-card"><div class="tool-card-head"><span class="tool-icon">⬡</span><div><strong>Servidores MCP</strong><small>${catalogSummary("mcp_servers", "mcp", "servidor", "servidores")}</small></div></div><button class="secondary-button" data-open-tools-tab="mcp">Gerenciar MCP</button></article>
  </div>${!full.installed ? '<div class="inline-notice">Instale a experiência completa em Configurações para habilitar navegador e desktop.</div>' : ""}${catalogNotice}`;
}

function renderSkillTools(data) {
  const skills = data.skills || [];
  selectors.toolsContent.innerHTML = skills.length ? `<div class="extension-list">${skills.map(item => {
    const name = extensionName(item, item.path);
    return `<article class="extension-row"><label><input type="checkbox" data-select-kind="skill" data-select-value="${escapeHTML(item.path)}" ${itemSelected("skill", item) ? "checked" : ""}><span><strong>${escapeHTML(name)}</strong><small>${escapeHTML(extensionDescription(item) || item.path)}</small></span></label><button class="ghost-button" data-extension-toggle="skill" data-extension-path="${escapeHTML(item.path)}" data-extension-enabled="${!skillEnabled(item)}">${skillEnabled(item) ? "Desativar globalmente" : "Ativar globalmente"}</button></article>`;
  }).join("")}</div>` : `<div class="panel-empty">Nenhuma skill foi encontrada neste projeto.${data.errors?.skills ? `<br>${escapeHTML(data.errors.skills)}` : ""}</div>`;
}

function renderAppTools(data) {
  const apps = data.apps || [];
  selectors.toolsContent.innerHTML = `${apps.length ? `<div class="extension-list">${apps.map(item => {
    const id = item.id || item.appId || item.name;
    const accessible = item.accessible ?? item.isAccessible ?? item.installed ?? true;
    const installUrl = item.installUrl || item.install_url || item.url;
    const runtime = item.callable === true ? "Pronto para uso" : item.callable === false ? "Sem ferramenta chamável no momento" : "";
    const description = extensionDescription(item) || (accessible ? `Identificador: ${id}` : "Requer instalação ou autorização");
    return `<article class="extension-row"><label><input type="checkbox" data-select-kind="app" data-select-value="${escapeHTML(id)}" ${itemSelected("app", item) ? "checked" : ""} ${!accessible ? "disabled" : ""}><span><strong>${escapeHTML(extensionName(item, id))}</strong><small>${escapeHTML(description)}${runtime ? ` • ${escapeHTML(runtime)}` : ""}</small></span></label><div class="row-actions">${!accessible && installUrl ? `<button class="secondary-button" data-open-url="${escapeHTML(installUrl)}">Instalar/autorizar</button>` : ""}<button class="ghost-button" data-extension-toggle="app" data-extension-name="${escapeHTML(id)}" data-extension-enabled="${!appEnabled(item)}">${appEnabled(item) ? "Desativar globalmente" : "Ativar globalmente"}</button></div></article>`;
  }).join("")}</div>` : `<div class="panel-empty">${data.errors?.apps ? `Não foi possível consultar os apps.<br>${escapeHTML(data.errors.apps)}` : "Nenhum app está instalado nesta conta do Codex."}</div>`}<div class="inline-notice">${escapeHTML(data.plugin_marketplace?.message || "Apps instalados pelo ecossistema do Codex aparecem aqui.")}</div>`;
}

function renderMcpTools(data) {
  const servers = data.mcp_servers || [];
  selectors.toolsContent.innerHTML = `${servers.length ? `<div class="extension-list">${servers.map(item => {
    const name = mcpName(item);
    const auth = item.authStatus || item.auth_status || item.oauthStatus || item.authentication?.status || "";
    const needsAuth = /required|login|unauth|expired/i.test(String(auth));
    return `<article class="extension-row"><label><input type="checkbox" data-select-kind="mcp" data-select-value="${escapeHTML(name)}" ${itemSelected("mcp", item) ? "checked" : ""}><span><strong>${escapeHTML(name)}</strong><small>${escapeHTML(item.description || item.status?.message || String(auth || "Servidor de ferramentas"))}</small></span></label><div class="row-actions">${needsAuth ? `<button class="secondary-button" data-mcp-oauth="${escapeHTML(name)}">Entrar</button>` : ""}<button class="ghost-button" data-extension-toggle="mcp" data-extension-name="${escapeHTML(name)}" data-extension-enabled="${!mcpEnabled(item)}">${mcpEnabled(item) ? "Desativar globalmente" : "Ativar globalmente"}</button></div></article>`;
  }).join("")}</div>` : `<div class="panel-empty">Nenhum servidor MCP foi listado.${data.errors?.mcp ? `<br>${escapeHTML(data.errors.mcp)}` : ""}</div>`}
  <details class="custom-mcp"><summary>Adicionar servidor MCP avançado</summary><div class="custom-mcp-form"><input id="mcp-custom-name" placeholder="Nome (ex.: meu_sistema)"><input id="mcp-custom-url" placeholder="URL HTTPS do servidor"><span>ou</span><input id="mcp-custom-command" placeholder="Executável local"><input id="mcp-custom-args" placeholder="Argumentos separados por espaço"><select id="mcp-custom-approval"><option value="prompt">Pedir aprovação</option><option value="writes">Aprovar leituras</option><option value="auto">Automático</option><option value="approve">Sempre aprovar</option></select><button class="primary-button" data-add-custom-mcp>Adicionar MCP</button></div></details>`;
}

function renderPluginTools(data) {
  const allPlugins = data.plugins || [];
  const query = state.pluginSearch.trim().toLowerCase();
  const matching = allPlugins.filter(item => !query || `${extensionName(item)} ${extensionDescription(item)} ${item.name || ""} ${item.marketplaceName || ""}`.toLowerCase().includes(query));
  const plugins = matching.slice(0, 250);
  const supported = data.plugin_marketplace?.production_install_supported !== false;
  selectors.toolsContent.innerHTML = `<div class="plugin-catalog-toolbar"><input type="search" data-plugin-search value="${escapeHTML(state.pluginSearch)}" placeholder="Pesquisar em ${allPlugins.length} plugins"><span>${matching.length} encontrado(s)${matching.length > plugins.length ? ` • mostrando ${plugins.length}` : ""}</span></div>${plugins.length ? `<div class="extension-list">${plugins.map(item => {
    const name = extensionName(item, item.name || item.id || "Plugin");
    const installed = item.installed === true;
    const enabled = item.enabled !== false;
    const unavailable = item.availability === "DISABLED_BY_ADMIN" || item.installPolicy === "NOT_AVAILABLE";
    const status = unavailable ? "Indisponível nesta conta" : installed ? (enabled ? "Instalado e ativo" : "Instalado, mas desativado") : "Disponível para instalar";
    const detail = extensionDescription(item) || item.interface?.longDescription || "Plugin do catálogo universal do Codex";
    const auth = installed ? "A conexão será solicitada se o plugin precisar acessar uma conta externa." : "Instalar não conecta contas externas automaticamente.";
    const marketplacePath = item.marketplacePath || "";
    const marketplaceName = item.marketplaceName || "";
    return `<article class="extension-row"><div><strong>${escapeHTML(name)}</strong><small>${escapeHTML(detail)}</small><div class="plugin-status-line"><span class="status-chip ${installed && enabled ? "ok" : unavailable ? "danger" : "warn"}">${escapeHTML(status)}</span><span class="plugin-marketplace">${escapeHTML(marketplaceName)} • ${escapeHTML(auth)}</span></div></div><div class="row-actions">${!installed && !unavailable && supported ? `<button class="primary-button" data-plugin-install="${escapeHTML(item.name || item.id)}" data-plugin-marketplace-path="${escapeHTML(marketplacePath)}" data-plugin-marketplace-name="${escapeHTML(marketplaceName)}">Instalar</button>` : ""}${installed ? `<span class="status-chip ${enabled ? "ok" : "warn"}">${enabled ? "Pronto" : "Desativado"}</span>` : ""}</div></article>`;
  }).join("")}</div>` : `<div class="panel-empty">O catálogo de plugins não retornou itens.${data.errors?.plugins ? `<br>${escapeHTML(data.errors.plugins)}` : ""}</div>`}<div class="inline-notice">${escapeHTML(data.plugin_marketplace?.message || "Plugins instalados podem adicionar skills, conectores e MCPs.")}</div>`;
}

async function installCatalogPlugin(button) {
  if (!state.activeProject) return;
  button.disabled = true;
  button.textContent = "Instalando…";
  try {
    const payload = {
      plugin_name: button.dataset.pluginInstall,
      marketplace_path: button.dataset.pluginMarketplacePath || null,
      remote_marketplace_name: button.dataset.pluginMarketplacePath ? null : (button.dataset.pluginMarketplaceName || null),
    };
    const data = await api(`/api/extensions/plugins/install?project_id=${encodeURIComponent(state.activeProject.id)}`, {method:"POST", body:JSON.stringify(payload)});
    const auth = data.result?.appsNeedingAuth || data.result?.apps_needing_auth || [];
    toast(auth.length ? "Plugin instalado. Conecte a conta externa quando solicitado." : "Plugin instalado. Abra uma nova conversa para usar seus recursos.", "success");
    await loadExtensions(true);
  } finally {
    button.disabled = false;
  }
}

function updateProfileSelection(kind, value, checked) {
  if (kind === "browser" || kind === "desktop") state.toolProfile[kind] = checked;
  else if (kind === "mcp") {
    const values = new Set(state.toolProfile.mcp_servers);
    checked ? values.add(value) : values.delete(value);
    state.toolProfile.mcp_servers = [...values];
  } else if (kind === "skill") {
    const item = (state.extensions?.skills || []).find(entry => entry.path === value);
    state.toolProfile.skills = state.toolProfile.skills.filter(entry => entry.path !== value);
    if (checked && item) state.toolProfile.skills.push({name:extensionName(item, value.split("/").pop()), path:value});
  } else if (kind === "app") {
    const item = (state.extensions?.apps || []).find(entry => (entry.id || entry.appId || entry.name) === value);
    state.toolProfile.apps = state.toolProfile.apps.filter(entry => entry.id !== value);
    if (checked && item) state.toolProfile.apps.push({id:value, name:extensionName(item, value), slug:item.slug || item.name || value});
  }
  state.toolProfile = normalizeToolProfile(state.toolProfile);
  renderToolProfileChip();
  selectors.toolsSummary.textContent = toolProfileCount() ? `${toolProfileCount()} recurso(s) associado(s)` : "Nenhuma ferramenta selecionada";
}

async function saveToolAssociation() {
  if (!state.activeProject) return;
  const threadId = state.toolsScope === "thread" ? state.activeThreadId : null;
  if (state.toolsScope === "thread" && !threadId) {
    toast("A seleção será aplicada ao criar esta nova conversa.", "success");
    selectors.toolsDialog.close();
    return;
  }
  const payload = {project_id:state.activeProject.id, thread_id:threadId, ...state.toolProfile};
  const data = await api("/api/tool-profile", {method:"PUT", body:JSON.stringify(payload)});
  state.toolProfile = normalizeToolProfile(data.profile);
  renderToolProfileChip();
  selectors.toolsDialog.close();
  toast(state.toolsScope === "project" ? "Padrão do projeto atualizado." : "Ferramentas da conversa atualizadas.", "success");
}

async function toggleExtension(kind, button) {
  const enabled = button.dataset.extensionEnabled === "true";
  const query = `project_id=${encodeURIComponent(state.activeProject.id)}`;
  if (kind === "skill") await api(`/api/extensions/skills/toggle?${query}`, {method:"POST", body:JSON.stringify({path:button.dataset.extensionPath, enabled})});
  else if (kind === "app") await api(`/api/extensions/apps/toggle?${query}`, {method:"POST", body:JSON.stringify({name:button.dataset.extensionName, enabled})});
  else await api(`/api/extensions/mcp/toggle?${query}`, {method:"POST", body:JSON.stringify({name:button.dataset.extensionName, enabled})});
  toast(enabled ? "Extensão ativada." : "Extensão desativada.", "success");
  await loadExtensions(true);
  await loadStatus();
}

async function startMcpOAuth(name) {
  const data = await api(`/api/extensions/mcp/oauth?project_id=${encodeURIComponent(state.activeProject.id)}`, {method:"POST", body:JSON.stringify({name, thread_id:state.activeThreadId})});
  const url = data.authorizationUrl || data.authUrl || data.url || data.authorization_url || data.result?.authorizationUrl;
  if (url) window.open(url, "_blank", "noopener");
  else toast("A autorização foi iniciada. Atualize o catálogo após concluir.", "success");
}

async function addCustomMcp() {
  const name = el("mcp-custom-name")?.value.trim();
  const url = el("mcp-custom-url")?.value.trim();
  const command = el("mcp-custom-command")?.value.trim();
  const args = (el("mcp-custom-args")?.value || "").match(/(?:[^\s"]+|"[^"]*")+/g)?.map(value => value.replace(/^"|"$/g, "")) || [];
  const approval_mode = el("mcp-custom-approval")?.value || "prompt";
  await api(`/api/extensions/mcp?project_id=${encodeURIComponent(state.activeProject.id)}`, {method:"POST", body:JSON.stringify({name, url:url || null, command:command || null, args, approval_mode})});
  toast("Servidor MCP adicionado.", "success");
  await loadExtensions(true);
}

// ---------------------------------------------------------------------------
// OneDrive folder browser for backups and projects
// ---------------------------------------------------------------------------

async function loadBackupFolders(path = "") {
  selectors.backupFolderList.innerHTML = '<div class="panel-empty">Carregando pastas do OneDrive…</div>';
  const base = state.oneDriveFolderMode === "projects" ? "/api/cloud" : "/api/backup-cloud";
  const data = await api(`${base}/folders?path=${encodeURIComponent(path)}`);
  state.backupFolderPath = data.current || "";
  state.backupFolderParent = data.parent || "";
  el("backup-folder-current").textContent = `OneDrive / ${state.backupFolderPath}`;
  el("backup-folder-up").disabled = !state.backupFolderPath;
  el("backup-folder-select").disabled = !state.backupFolderPath;
  selectors.backupFolderList.innerHTML = (data.folders || []).length
    ? data.folders.map(folder => `<button type="button" class="backup-folder-row" data-backup-folder="${escapeHTML(folder.path)}"><span class="backup-folder-icon">▱</span><span><strong>${escapeHTML(folder.name)}</strong><small>${escapeHTML(folder.path)}</small></span><span>›</span></button>`).join("")
    : '<div class="panel-empty">Esta pasta não contém outras pastas.</div>';
}

async function openOneDriveFolderBrowser(mode = "backup") {
  state.oneDriveFolderMode = ["projects", "project-root"].includes(mode) ? mode : "backup";
  el("backup-folder-title").textContent = state.oneDriveFolderMode === "project-root"
    ? "Escolher a pasta raiz dos projetos no OneDrive"
    : state.oneDriveFolderMode === "projects"
      ? "Escolher pasta dos projetos no OneDrive"
      : "Escolher pasta de backup no OneDrive";
  el("backup-folder-select").textContent = state.oneDriveFolderMode === "project-root"
    ? "Sincronizar e usar esta pasta"
    : state.oneDriveFolderMode === "projects"
      ? "Usar para projetos"
      : "Usar para backup";
  closeMobilePanels();
  if (selectors.settingsDialog.open) selectors.settingsDialog.close();
  if (!selectors.backupFolderDialog.open) selectors.backupFolderDialog.showModal();
  await loadBackupFolders("");
}

async function createBackupFolder() {
  const name = el("backup-folder-new-name").value.trim();
  if (!name) return toast("Informe o nome da nova pasta.", "error");
  const base = state.oneDriveFolderMode === "projects" ? "/api/cloud" : "/api/backup-cloud";
  const data = await api(`${base}/folders`, {method:"POST", body:JSON.stringify({parent:state.backupFolderPath, name})});
  el("backup-folder-new-name").value = "";
  await loadBackupFolders(data.created || state.backupFolderPath);
  toast("Pasta criada no OneDrive.", "success");
}

async function selectBackupFolder() {
  if (!state.backupFolderPath) return toast("Abra ou crie uma pasta para selecioná-la.", "error");
  if (state.oneDriveFolderMode === "project-root") {
    selectors.backupFolderDialog.close();
    if (selectors.projectRootDialog.open) selectors.projectRootDialog.close();
    if (selectors.projectDialog.open) selectors.projectDialog.close();
    return startBackgroundTask("/api/projects/onedrive-root", {remote_path:state.backupFolderPath});
  } else if (state.oneDriveFolderMode === "projects") {
    const local_path = el("settings-cloud-local-path")?.value.trim()
      || el("cloud-local-path")?.value.trim()
      || state.lastStatus?.cloud_sync?.local_path
      || state.setup.data?.cloud_sync?.local_path;
    await api("/api/cloud/folder", {method:"POST", body:JSON.stringify({local_path, remote_path:state.backupFolderPath})});
  } else {
    await api("/api/backup-cloud/activate", {method:"POST", body:JSON.stringify({remote_path:state.backupFolderPath})});
  }
  selectors.backupFolderDialog.close();
  if (state.setup.active) await refreshSetupState();
  else await loadStatus();
  toast(`${state.oneDriveFolderMode === "projects" ? "Projetos configurados" : "Backup configurado"} em OneDrive / ${state.backupFolderPath}`, "success");
}

// Authenticated Codex terminal
// ---------------------------------------------------------------------------

function terminalSend(data) {
  if (state.terminalSocket?.readyState !== WebSocket.OPEN) {
    toast("O terminal ainda não está conectado.", "error");
    return false;
  }
  state.terminalSocket.send(JSON.stringify({type:"input", data}));
  return true;
}

function stripTerminalAnsi(value) {
  return String(value || "")
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, "")
    .replace(/\u001b\[[0-?]*[ -\/]*[@-~]/g, "")
    .replace(/\u001b[@-_]/g, "")
    .replace(/\r/g, "");
}

function appendTerminalOutput(value) {
  const output = selectors.terminalOutput;
  output.textContent += stripTerminalAnsi(value);
  if (output.textContent.length > 250000) output.textContent = output.textContent.slice(-200000);
  output.scrollTop = output.scrollHeight;
}

function terminalGeometry() {
  const rect = selectors.terminalOutput.getBoundingClientRect();
  return {cols:Math.max(40, Math.floor(rect.width / 8.3)), rows:Math.max(12, Math.floor(rect.height / 20))};
}

function closeTerminalSocket() {
  const socket = state.terminalSocket;
  state.terminalSocket = null;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "terminal fechado");
  selectors.terminalState.textContent = "Desconectado";
  selectors.terminalState.className = "status-chip warn";
}

function connectTerminal(workspaceOverride = state.terminalWorkspace || activeWorkspace()) {
  closeTerminalSocket();
  const workspace = workspaceOverride;
  state.terminalWorkspace = workspace;
  const params = new URLSearchParams({workspace});
  if (workspace === "projects") {
    if (!state.activeProject || state.activeProject.kind === "system") {
      toast("Selecione um projeto antes de abrir o terminal isolado.", "error");
      return;
    }
    params.set("project_id", state.activeProject.id);
  }
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${scheme}//${location.host}/api/terminal/ws?${params}`);
  state.terminalSocket = socket;
  selectors.terminalState.textContent = "Conectando";
  selectors.terminalState.className = "status-chip warn";
  const emergency = workspace === "codex-emergency";
  el("terminal-scope").textContent = emergency ? "Codex Sistema • emergência" : workspace === "system" ? "Codex do Sistema" : state.activeProject.name;
  el("terminal-privilege").textContent = emergency ? "Codex do Sistema • sudo sem senha" : workspace === "system" ? "root no host • sudo sem senha" : "Codex de Projetos • sem sudo no host";
  el("terminal-prompt").textContent = emergency ? "›" : "$";
  selectors.terminalInput.placeholder = emergency ? "Diga ao Codex o que deseja verificar, operar ou reparar" : "Digite um comando";
  selectors.terminalInput.setAttribute("aria-label", emergency ? "Instrução para o Codex Sistema" : "Comando do terminal");
  el("terminal-submit").textContent = emergency ? "Enviar ao Codex" : "Executar";
  socket.addEventListener("open", () => {
    const size = terminalGeometry();
    socket.send(JSON.stringify({type:"resize", ...size}));
  });
  socket.addEventListener("message", event => {
    let message;
    try { message = JSON.parse(event.data); } catch { appendTerminalOutput(event.data); return; }
    if (message.type === "output") {
      const terminalData = String(message.data || "");
      if (terminalData.includes("\u001b]10;?\u001b\\")) terminalSend("\u001b]10;rgb:eeee/eeee/eeee\u001b\\");
      if (terminalData.includes("\u001b]11;?\u001b\\")) terminalSend("\u001b]11;rgb:0a0a/0f0f/1717\u001b\\");
      appendTerminalOutput(terminalData);
    }
    if (message.type === "ready") {
      selectors.terminalState.textContent = "Conectado";
      selectors.terminalState.className = "status-chip ok";
      el("terminal-title").textContent = message.label || "Terminal do Codex";
      selectors.terminalInput.focus();
    }
    if (message.type === "error") appendTerminalOutput(`\n[erro] ${message.message}\n`);
  });
  socket.addEventListener("close", event => {
    if (state.terminalSocket !== socket) return;
    state.terminalSocket = null;
    selectors.terminalState.textContent = event.code === 4403 ? "Acesso recusado" : "Desconectado";
    selectors.terminalState.className = "status-chip warn";
    if (event.code === 4403) appendTerminalOutput("\nA sessão autenticada não autorizou este terminal. Atualize a página e entre novamente.\n");
  });
  socket.addEventListener("error", () => appendTerminalOutput("\nFalha ao conectar o terminal.\n"));
}

function openShellTerminal() {
  closeMobilePanels();
  selectors.terminalOutput.textContent = "";
  state.terminalWorkspace = activeWorkspace();
  if (!selectors.terminalDialog.open) selectors.terminalDialog.showModal();
  connectTerminal(state.terminalWorkspace);
}

function openEmergencyCodex() {
  closeMobilePanels();
  selectors.terminalOutput.textContent = "";
  state.terminalWorkspace = "codex-emergency";
  if (!selectors.terminalDialog.open) selectors.terminalDialog.showModal();
  connectTerminal("codex-emergency");
}

function openTerminal() {
  openEmergencyCodex();
}

function populateRemoteEnvironmentChooser() {
  const select = selectors.remoteProjectSelect;
  if (!select) return [];
  const projects = state.projects.filter(project => project.kind !== "system");
  const preferred = state.activeProject?.kind !== "system" ? state.activeProject.id : select.value;
  select.replaceChildren();
  if (!projects.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Nenhum projeto disponível";
    select.append(option);
  } else {
    for (const project of projects) {
      const option = document.createElement("option");
      option.value = project.id;
      option.textContent = project.name;
      select.append(option);
    }
    if (projects.some(project => project.id === preferred)) select.value = preferred;
  }
  const terminal = selectors.remoteEnvironmentDialog?.querySelector('[data-remote-entry="projects-terminal"]');
  if (terminal) terminal.disabled = !projects.length;
  select.disabled = !projects.length;
  return projects;
}

function openRemoteEnvironmentChooser() {
  closeMobilePanels();
  populateRemoteEnvironmentChooser();
  if (!selectors.remoteEnvironmentDialog.open) selectors.remoteEnvironmentDialog.showModal();
}

function closeRemoteEnvironmentChooser() {
  if (selectors.remoteEnvironmentDialog?.open) selectors.remoteEnvironmentDialog.close();
}

async function launchRemoteEnvironment(entry) {
  if (entry === "system-terminal") {
    closeRemoteEnvironmentChooser();
    openEmergencyCodex();
    return;
  }
  if (entry === "projects-terminal") {
    const projectId = selectors.remoteProjectSelect?.value || "";
    if (!projectId) {
      toast("Selecione um projeto para abrir o Codex Terminal.", "error");
      return;
    }
    await selectProject(projectId);
    closeRemoteEnvironmentChooser();
    openShellTerminal();
    return;
  }
  closeRemoteEnvironmentChooser();
  if (entry === "system-graphical") return openRemoteDesktop({target:"codex", application:"codex-system", title:"Codex Sistema • interface gráfica"});
  if (entry === "projects-graphical") return openRemoteDesktop({target:"codex", application:"codex-projects", title:"Codex Projetos • interface gráfica"});
  if (entry === "android-graphical") return openRemoteDesktop({liveAndroid:true, title:"Android • Waydroid"});
  if (entry === "desktop-graphical") return openRemoteDesktop({target:"desktop", title:"Desktop Ubuntu"});
  if (entry === "games-graphical") return openRemoteDesktop({target:"jogos", title:"Steam Machine / HDMI"});
}

// Secure pairing and adaptive remote workspace
// ---------------------------------------------------------------------------

async function showPairingDialog() {
  if (state.identity !== "localhost") {
    toast("O QR Code somente pode ser criado diretamente no computador Linux.", "error");
    return;
  }
  try {
    const data = await api("/api/security/pairing", {method:"POST"});
    selectors.pairingQr.src = data.qr_data_url;
    selectors.pairingLink.href = data.pairing_url;
    selectors.pairingLink.textContent = data.pairing_url;
    selectors.pairingExpiry.dataset.expiresAt = String(data.expires_at || 0);
    updatePairingExpiry();
    clearInterval(state.pairingTimer);
    state.pairingTimer = setInterval(updatePairingExpiry, 1000);
    if (!selectors.pairingDialog.open) selectors.pairingDialog.showModal();
  } catch (error) { toast(error.message, "error"); }
}

function updatePairingExpiry() {
  const expiresAt = Number(selectors.pairingExpiry?.dataset.expiresAt || 0);
  const seconds = Math.max(0, Math.floor(expiresAt - Date.now() / 1000));
  if (selectors.pairingExpiry) selectors.pairingExpiry.textContent = seconds ? `Expira em ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}` : "Código expirado";
  if (!seconds && state.pairingTimer) { clearInterval(state.pairingTimer); state.pairingTimer = null; }
}

async function closePairingDialog(refresh = true) {
  clearInterval(state.pairingTimer);
  state.pairingTimer = null;
  selectors.pairingDialog.close();
  if (!refresh) return;
  if (state.setup.active) await refreshSetupState().catch(() => null);
  else await loadStatus().catch(() => null);
}

async function loadPairedDevices() {
  try {
    const data = await api("/api/security/devices");
    state.devices = data.devices || [];
    state.enrollmentRequests = data.enrollment_requests || [];
    state.localNetworkAdmin = Boolean(data.local_network_admin);
    state.deviceAdmin = Boolean(data.device_admin);
    state.deviceLimit = Number(data.device_limit || 6);
  } catch (error) {
    state.devices = [];
    state.enrollmentRequests = [];
    state.localNetworkAdmin = false;
    state.deviceAdmin = false;
    state.deviceLimit = 6;
    addActivity("Dispositivos", error.message, "error");
  }
  return state.devices;
}

async function decideEnrollmentRequest(requestId, decision) {
  await api(`/api/security/device/enrollment/${encodeURIComponent(requestId)}/${decision}`, {method:"POST"});
  await loadPairedDevices();
  if (state.lastStatus) renderSettings(state.lastStatus);
  toast(decision === "approve" ? "Dispositivo aprovado." : "Solicitação recusada.", "success");
}

async function revokePairedDevice(deviceId) {
  const device = state.devices.find(item => item.id === deviceId);
  if (!window.confirm(`Revogar o acesso de ${device?.name || "este dispositivo"}? A sessão remota será encerrada.`)) return;
  await api(`/api/security/devices/${encodeURIComponent(deviceId)}`, {method:"DELETE"});
  await loadPairedDevices();
  if (state.lastStatus) renderSettings(state.lastStatus);
  toast("Dispositivo revogado.", "success");
}

function remoteDeviceInfo(profileOverride = selectors.remoteProfile?.value || "auto") {
  const viewport = window.visualViewport;
  const stage = el("remote-stage");
  const interfaceScale = effectiveRemoteScale();
  const width = Math.max(240, Math.round((stage?.clientWidth || viewport?.width || window.innerWidth || 1440) / interfaceScale));
  const height = Math.max(320, Math.round((stage?.clientHeight || viewport?.height || window.innerHeight || 900) / interfaceScale));
  const detectedTouch = navigator.maxTouchPoints > 0 || matchMedia("(pointer: coarse)").matches;
  const activeRemoteTarget = state.remote.sessionTarget || state.remote.target || "codex";
  const ubuntuTouchSession = ["codex", "projects", "desktop"].includes(activeRemoteTarget);
  const touch = ubuntuTouchSession || detectedTouch;
  let deviceType = profileOverride;
  const handsetViewport = detectedTouch
    && Math.min(viewport?.width || window.innerWidth || width, viewport?.height || window.innerHeight || height) <= 700;
  if (activeRemoteTarget === "playwright" && handsetViewport) deviceType = "phone";
  if (deviceType === "auto") {
    const shortest = Math.min(width, height);
    const longest = Math.max(width, height);
    deviceType = touch && shortest <= 600 && longest <= 1200 ? "phone" : touch && shortest <= 1024 && longest <= 1600 ? "tablet" : "desktop";
  }
  // A sessão Ubuntu remota usa sempre o perfil touch. Em uma tela grande o
  // perfil tablet preserva espaço de trabalho sem voltar ao comportamento
  // de ponteiro e aos alvos pequenos da versão desktop.
  if (ubuntuTouchSession && !["phone", "tablet"].includes(deviceType)) deviceType = "tablet";
  const orientation = height >= width ? "portrait" : "landscape";
  return {
    target:activeRemoteTarget,
    viewport_width:width,
    viewport_height:height,
    device_type:deviceType,
    orientation,
    touch,
    device_pixel_ratio:Math.max(0.5, Math.min(Number(window.devicePixelRatio || 1), 4)),
  };
}

function remoteResizeSignature(info) {
  const target = String(info?.target || state.remote.sessionTarget || state.remote.target || "codex");
  const profile = String(info?.device_type || "auto").toLowerCase();
  const orientation = String(info?.orientation || "auto");
  if (["phone", "mobile", "celular", "tablet", "tab"].includes(profile)) {
    return `${target}:${profile}:${orientation}`;
  }
  return `${target}:${profile}:${orientation}:${info?.viewport_width || 0}x${info?.viewport_height || 0}`;
}

function describeRemoteStatus(status) {
  if (!status) return "Tela remota indisponível.";
  if (status.session_type === "waydroid-android-vnc") {
    return status.running ? "Android 13 • sessão privada ao vivo" : (status.reason || "Android ao vivo indisponível.");
  }
  if (status.session_type === "ubuntu-gnome-wayland-headless") {
    return `Codex • GNOME Ubuntu/Wayland • ${status.running ? "sessão preparada" : "disponível"}`;
  }
  if (status.session_type === "ubuntu-openbox-xvnc") {
    return `Codex Projetos • perfil Ubuntu isolado • ${status.running ? "sessão preparada" : "disponível"}`;
  }
  if (status.physical) {
    if (!status.available) return status.reason || `A sessão física ${status.target || ""} ainda não está ativa.`;
    const protocol = status.target === "desktop" ? "GNOME/Wayland via RDP privado" : (status.session_type || "x11");
    return `${status.target === "jogos" ? "Steam Machine / HDMI" : "Desktop Ubuntu"} • ${protocol} • ${status.running ? "sessão preparada" : "disponível"}`;
  }
  if (!status.available) return "Componentes da tela remota ainda não estão instalados.";
  if (!status.running) return "GNOME Ubuntu do Codex pronto para iniciar.";
  const geometry = status.geometry || {};
  const profile = geometry.profile === "phone" ? "celular" : geometry.profile === "tablet" ? "tablet" : "computador";
  return `${geometry.width || "?"} × ${geometry.height || "?"} • perfil ${profile} • ${status.clients || 0} conexão(ões)`;
}

async function refreshRemoteStatus() {
  try {
    const status = await api(`/api/remote-desktop/status?target=${encodeURIComponent(state.remote.target || "codex")}`);
    state.remote.status = status;
    const legacyViewerOpen = state.remote.dialogOpen
      && state.remote.target === "codex"
      && selectors.remoteFrame?.src.includes("/novnc/");
    if (legacyViewerOpen && status.session_type === "ubuntu-gnome-wayland-headless") {
      const started = await api("/api/remote-desktop/start", {method:"POST", body:JSON.stringify(remoteDeviceInfo())});
      state.remote.status = started;
      loadRemoteViewer(started.viewer_url);
      return started;
    }
    if (selectors.remoteSummaryStatus) selectors.remoteSummaryStatus.textContent = describeRemoteStatus(status);
    if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = describeRemoteStatus(status);
    el("open-remote-desktop").disabled = !status.enabled || !status.available;
    el("open-remote-browser").disabled = !status.enabled || !status.available;
    return status;
  } catch (error) {
    if (selectors.remoteSummaryStatus) selectors.remoteSummaryStatus.textContent = error.message;
    if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = error.message;
    return null;
  }
}

function setRemotePlaceholder(visible, title = "Iniciando a sessão gráfica segura", detail = "A resolução será escolhida automaticamente para este dispositivo.") {
  if (!selectors.remotePlaceholder) return;
  selectors.remotePlaceholder.classList.toggle("hidden", !visible);
  const strong = selectors.remotePlaceholder.querySelector("strong");
  const small = selectors.remotePlaceholder.querySelector("small");
  if (strong) strong.textContent = title;
  if (small) small.textContent = detail;
}

function loadRemoteViewer(viewerUrl) {
  state.remote.frameReady = false;
  resetRemoteViewport(false);
  setRemotePlaceholder(true);
  applyRemoteInterfaceScale();
  const hashIndex = viewerUrl.indexOf("#");
  const baseUrl = hashIndex >= 0 ? viewerUrl.slice(0, hashIndex) : viewerUrl;
  const fragment = hashIndex >= 0 ? viewerUrl.slice(hashIndex) : "";
  const finalUrl = `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}_=${Date.now()}${fragment}`;
  if (baseUrl.startsWith("/guacamole/")) {
    selectors.remoteFrame.src = `/remote-guacamole-reset.html?next=${encodeURIComponent(finalUrl)}`;
    return;
  }
  selectors.remoteFrame.src = finalUrl;
}

function captureRemoteStableViewport() {
  const viewport = window.visualViewport;
  const height = Math.max(320, Math.round(Math.max(viewport?.height || 0, window.innerHeight || 0)));
  state.remote.stableViewportHeight = height;
  document.documentElement.style.setProperty("--remote-stable-height", `${height}px`);
  return height;
}

async function openRemoteDesktop(options = {}) {
  const launchBrowser = Boolean(options.browser);
  const liveBrowser = Boolean(options.liveBrowser);
  const liveAndroid = Boolean(options.liveAndroid);
  const launchApplication = String(options.application || "");
  const browserSession = launchBrowser || liveBrowser || liveAndroid;
  const browserUrl = String(options.url || "about:blank");
  const remoteTarget = liveBrowser ? "playwright" : liveAndroid ? "android" : (options.target || state.remote.target || "codex");
  const sameOpenSession = state.remote.dialogOpen
    && state.remote.sessionTarget === remoteTarget
    && selectors.remoteFrame?.src
    && !selectors.remoteFrame.src.endsWith("about:blank");
  if ((sameOpenSession || state.remote.opening) && !options.forceReload) {
    if (!selectors.remoteDialog.open) selectors.remoteDialog.showModal();
    state.remote.dialogOpen = true;
    document.body.classList.add("remote-open");
    setRemoteToolbarExpanded(false);
    return state.remote.status;
  }
  state.remote.opening = true;
  resetRemoteKeyboardTransport(true);
  state.remote.sessionTarget = remoteTarget;
  renderRemoteGameControl();
  state.remote.mobileLayout = false;
  if (!options.preserveMobileLayoutPending) state.remote.mobileLayoutPending = false;
  renderRemoteMobileLayoutControl(false);
  if (options.target && !liveBrowser && !liveAndroid) state.remote.target = options.target;
  if (selectors.remoteTarget) selectors.remoteTarget.value = state.remote.target;
  state.remote.launchBrowserOnOpen = launchBrowser;
  setRemoteToolbarExpanded(false);
  selectors.remoteDialog.classList.toggle("browser-session", browserSession);
  // O acesso remoto comum segue o visor ao vivo: toda a janela pertence à
  // transmissão e os controles ficam sobrepostos em um menu flutuante.
  selectors.remoteDialog.classList.add("immersive");
  el("remote-fullscreen")?.setAttribute("aria-pressed", String(Boolean(document.fullscreenElement)));
  const remoteTitle = el("remote-dialog-title");
  if (remoteTitle) remoteTitle.textContent = String(options.title || (liveAndroid ? "Android da conversa ao vivo" : liveBrowser ? "Navegador da conversa ao vivo" : launchBrowser ? "Navegador compartilhado" : "Área de trabalho Linux"));
  if (selectors.remoteFrame) selectors.remoteFrame.title = liveAndroid ? "Android da conversa, visualização e controle em tempo real" : browserSession ? "Navegador da conversa, visualização e controle em tempo real" : "Tela interativa do computador Linux";
  if (launchBrowser && selectors.remoteProfile?.value === "auto") {
    const touchPhone = (navigator.maxTouchPoints > 0 || matchMedia("(pointer: coarse)").matches)
      && Math.min(window.innerWidth, window.innerHeight) <= 700;
    if (touchPhone) selectors.remoteProfile.value = "phone";
  }
  captureRemoteStableViewport();
  if (!selectors.remoteDialog.open) selectors.remoteDialog.showModal();
  state.remote.dialogOpen = true;
  document.body.classList.add("remote-open");
  setRemotePlaceholder(true);
  selectors.remoteLiveStatus.textContent = liveAndroid ? "Conectando à sessão Android privada…" : liveBrowser ? "Preparando a janela Playwright isolada…" : state.remote.target === "codex" ? "Preparando o GNOME Ubuntu do Codex…" : state.remote.target === "projects" ? "Preparando o perfil Ubuntu do Codex de Projetos…" : state.remote.target === "desktop" ? "Preparando o GNOME padrão remoto no mini PC…" : "Trocando o perfil do HDMI para jogos sem reiniciar o computador…";
  if (liveAndroid) {
    setRemotePlaceholder(true, "Preparando o Android ao vivo", "A mesma sessão Waydroid usada na automação será exibida com toque, mouse e teclado.");
  }
  if (state.remote.target !== "codex") {
    const projects = state.remote.target === "projects";
    setRemotePlaceholder(true, projects ? "Preparando o Codex de Projetos" : state.remote.target === "desktop" ? "Preparando o Desktop Ubuntu" : "Preparando a tela de jogos", projects ? "Uma sessão privada do usuário codex-worker será aberta sem interromper os workers nem a VM." : state.remote.target === "desktop" ? "Uma sessão GNOME padrão segura será aberta no mini PC; servidor e Codex continuam ativos." : "A sessão do HDMI será trocada para o perfil de jogos; servidor e Codex continuam ativos.");
  }
  try {
    const payload = {
      ...remoteDeviceInfo(),
      target:remoteTarget,
      thread_id:liveBrowser ? String(options.threadId || state.activeThreadId || "") : "",
    };
    const status = await api("/api/remote-desktop/start", {method:"POST", body:JSON.stringify(payload)});
    state.remote.status = status;
    state.remote.lastResizeSignature = remoteResizeSignature(payload);
    loadRemoteViewer(status.viewer_url);
    selectors.remoteLiveStatus.textContent = describeRemoteStatus(status);
    if (remoteTarget === "playwright") await syncRemoteMobileLayoutControl();
    if (launchApplication && remoteTarget === "codex") {
      await api("/api/remote-desktop/launch", {
        method:"POST",
        body:JSON.stringify({...payload, application:launchApplication, browser_mode:selectors.remoteProfile.value || "auto", url:"about:blank"}),
      });
      selectors.remoteLiveStatus.textContent = launchApplication === "codex-projects"
        ? "Codex Projetos aberto com interface gráfica."
        : "Codex Sistema aberto com interface gráfica.";
    }
    if (launchBrowser && state.remote.target === "codex") {
      await api("/api/remote-desktop/launch", {
        method:"POST",
        body:JSON.stringify({...payload, application:"browser", browser_mode:selectors.remoteProfile.value || "auto", url:browserUrl}),
      });
      selectors.remoteLiveStatus.textContent = `${describeRemoteStatus(status)} • navegador adaptado aberto`;
    }
  } catch (error) {
    setRemotePlaceholder(true, "Não foi possível iniciar a tela remota", error.message);
    selectors.remoteLiveStatus.textContent = error.message;
    toast(error.message, "error");
  } finally {
    state.remote.opening = false;
  }
}

function closeRemoteDialog() {
  const closedSessionTarget = state.remote.sessionTarget;
  setRemoteToolbarExpanded(false);
  selectors.remoteDialog.classList.remove("immersive");
  selectors.remoteDialog.classList.remove("browser-session");
  if (document.fullscreenElement) document.exitFullscreen().catch(() => null);
  if (selectors.remoteDialog.open) selectors.remoteDialog.close();
  state.remote.dialogOpen = false;
  resetRemoteKeyboardTransport(true);
  clearInterval(state.remote.bridgeTimer);
  state.remote.bridgeTimer = null;
  hideRemoteKeyboard();
  if (selectors.remoteFrame && ["playwright", "android"].includes(closedSessionTarget)) {
    selectors.remoteFrame.src = "about:blank";
    state.remote.frameReady = false;
    state.remote.sessionTarget = "";
  }
  document.body.classList.remove("remote-open");
}

async function stopRemoteDesktop() {
  try {
    await api(`/api/remote-desktop/stop?target=${encodeURIComponent(state.remote.target || "codex")}`, {method:"POST"});
    selectors.remoteFrame.src = "about:blank";
    setRemotePlaceholder(true, "Sessão gráfica encerrada", "Abra novamente quando precisar.");
    closeRemoteDialog();
    await refreshRemoteStatus();
    toast("Sessão gráfica encerrada.", "success");
  } catch (error) { toast(error.message, "error"); }
}

async function launchRemoteApplication(application) {
  try {
    if (state.remote.target !== "codex") throw new Error("Use a própria tela física para abrir aplicativos nesta sessão.");
    const payload = remoteDeviceInfo();
    const body = {...payload, application, browser_mode:selectors.remoteProfile.value || "auto", url:"about:blank"};
    await api("/api/remote-desktop/launch", {method:"POST", body:JSON.stringify(body)});
    selectors.remoteLiveStatus.textContent = application === "browser" ? "Navegador adaptado aberto." : "Gerenciador de arquivos aberto.";
  } catch (error) { toast(error.message, "error"); }
}

async function fitRemoteDesktop() {
  if (!state.remote.dialogOpen) return;
  clearTimeout(state.remote.resizeTimer);
  state.remote.resizeTimer = setTimeout(async () => {
    try {
      const deviceInfo = remoteDeviceInfo();
      const signature = remoteResizeSignature(deviceInfo);
      if (signature === state.remote.lastResizeSignature) return;
      const status = await api("/api/remote-desktop/resize", {method:"POST", body:JSON.stringify(deviceInfo)});
      state.remote.lastResizeSignature = signature;
      state.remote.status = status;
      selectors.remoteLiveStatus.textContent = describeRemoteStatus(status);
    } catch (error) { selectors.remoteLiveStatus.textContent = error.message; }
  }, 220);
}

function remoteViewerDocuments() {
  const documents = [];
  const visit = frame => {
    try {
      const doc = frame?.contentDocument;
      if (!doc || documents.includes(doc)) return;
      documents.push(doc);
      doc.querySelectorAll("iframe").forEach(visit);
    } catch { /* A protected nested frame is simply not keyboard-addressable. */ }
  };
  visit(selectors.remoteFrame);
  return documents;
}

function remoteKeyboardDocument() {
  return remoteViewerDocuments()[0] || null;
}

function remoteKeyboardInput() {
  const query = [
    "#noVNC_keyboardinput",
    ".text-input textarea",
    ".keyboard-input textarea",
    "textarea[ng-model]",
    "textarea[autocapitalize='off']",
    "input[autocapitalize='off']",
    "textarea",
  ].join(",");
  for (const doc of remoteViewerDocuments()) {
    const input = doc.querySelector(query);
    if (input) return input;
  }
  return null;
}

function clearRemoteKeyboardAckTimer() {
  clearTimeout(state.remote.keyboardAckTimer);
  state.remote.keyboardAckTimer = null;
}

function resetRemoteKeyboardTransport(discard = false) {
  clearRemoteKeyboardAckTimer();
  state.remote.keyboardViewerReady = false;
  state.remote.keyboardInFlight = null;
  if (discard) state.remote.keyboardQueue = [];
}

function flushRemoteKeyboardQueue() {
  if (!state.remote.keyboardViewerReady || state.remote.keyboardInFlight || !state.remote.keyboardQueue.length) return;
  const viewer = selectors.remoteFrame?.contentWindow;
  if (!viewer) return;
  const item = state.remote.keyboardQueue[0];
  state.remote.keyboardInFlight = item;
  item.attempts = Number(item.attempts || 0) + 1;
  viewer.postMessage({type:"sasocq-remote-keyboard", id:item.id, ...item.payload}, location.origin);
  clearRemoteKeyboardAckTimer();
  state.remote.keyboardAckTimer = setTimeout(() => {
    if (state.remote.keyboardInFlight?.id !== item.id) return;
    state.remote.keyboardInFlight = null;
    if (item.attempts >= 3) {
      state.remote.keyboardViewerReady = false;
      if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = "Reconectando a entrada do teclado…";
      return;
    }
    setTimeout(flushRemoteKeyboardQueue, 120);
  }, 1200);
}

function postNativeRemoteKeyboard(payload) {
  try {
    const viewer = selectors.remoteFrame?.contentWindow;
    const frameUrl = new URL(selectors.remoteFrame?.src || "about:blank", location.href);
    if (!viewer || frameUrl.pathname !== "/remote-viewer.html") return false;
    const id = `keyboard-${Date.now()}-${++state.remote.keyboardSequence}`;
    state.remote.keyboardQueue.push({id, payload:{...payload}, attempts:0});
    if (state.remote.keyboardQueue.length > 500) state.remote.keyboardQueue.splice(0, state.remote.keyboardQueue.length - 500);
    flushRemoteKeyboardQueue();
    return true;
  } catch {
    return false;
  }
}

function postNativeRemotePointer(payload) {
  try {
    const viewer = selectors.remoteFrame?.contentWindow;
    if (!viewer || viewer.location.pathname !== "/remote-viewer.html") return false;
    viewer.postMessage({type:"sasocq-remote-pointer", ...payload}, location.origin);
    return true;
  } catch {
    return false;
  }
}

function handleRemoteKeyboardBridgeMessage(data) {
  if (data?.type === "sasocq-remote-status") {
    if (selectors.remoteLiveStatus && data.message) selectors.remoteLiveStatus.textContent = String(data.message);
    return true;
  }
  if (data?.type === "sasocq-remote-pointer-capabilities") {
    const stage = el("remote-stage");
    if (stage) {
      stage.dataset.relativePointer = String(Boolean(data.relative));
      stage.dataset.scrollX = String(Boolean(data.scrollX));
      stage.dataset.scrollY = String(Boolean(data.scrollY));
    }
    return true;
  }
  if (data?.type === "sasocq-remote-tap") {
    scheduleRemoteUbuntuKeyboardAutoHide(data.x, data.y, data.target);
    return true;
  }
  if (data?.type === "sasocq-remote-diagnostic") {
    console.debug("[visor remoto]", data.stage || "evento", data.detail || "");
    return true;
  }
  if (data?.type === "sasocq-remote-keyboard-state") {
    state.remote.keyboardViewerReady = Boolean(data.ready);
    if (!state.remote.keyboardViewerReady) {
      clearRemoteKeyboardAckTimer();
      state.remote.keyboardInFlight = null;
    }
    flushRemoteKeyboardQueue();
    return true;
  }
  if (data?.type === "sasocq-remote-keyboard-ack") {
    const id = String(data.id || "");
    if (!id || state.remote.keyboardInFlight?.id !== id) return true;
    clearRemoteKeyboardAckTimer();
    state.remote.keyboardInFlight = null;
    if (data.sent) {
      if (state.remote.keyboardQueue[0]?.id === id) state.remote.keyboardQueue.shift();
      else state.remote.keyboardQueue = state.remote.keyboardQueue.filter(item => item.id !== id);
    } else {
      state.remote.keyboardViewerReady = false;
    }
    flushRemoteKeyboardQueue();
    return true;
  }
  return false;
}

function installRemotePointerStateBridge() {
  if (state.remote.pointerStateBridgeInstalled) return;
  state.remote.pointerStateBridgeInstalled = true;
  window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== selectors.remoteFrame?.contentWindow) return;
    if (handleRemoteKeyboardBridgeMessage(event.data)) return;
    if (event.data?.type !== "sasocq-remote-pointer-state") return;
    const x = Number(event.data.x);
    const y = Number(event.data.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    state.remote.cursorX = x;
    state.remote.cursorY = y;
    const doc = remoteKeyboardDocument();
    if (doc) updateRemoteCursor(doc, x, y);
  });
}

async function showRemoteUbuntuKeyboard(options = {}) {
  const target = state.remote.sessionTarget || state.remote.target || "codex";
  hideRemoteKeyboard({preserveMode:true});
  if (target === "playwright") {
    const status = await api("/api/remote-desktop/keyboard?target=playwright&toggle=true", {method:"POST"});
    setRemoteKeyboardMode(status.visible ? "ubuntu" : "none");
    selectors.remoteLiveStatus.textContent = status.visible ? "Teclado touch do Ubuntu aberto." : "Teclado touch do Ubuntu ocultado.";
    setRemoteToolbarExpanded(false);
    return;
  }
  if (target !== "codex") throw new Error("O teclado touch do Ubuntu não está disponível nesta sessão.");
  try {
    const viewer = selectors.remoteFrame?.contentWindow;
    if (!viewer || viewer.location.pathname !== "/remote-viewer.html") throw new Error("Visor nativo indisponível");
    viewer.postMessage({type:"sasocq-remote-native-keyboard"}, location.origin);
    setRemoteKeyboardMode("none");
    selectors.remoteLiveStatus.textContent = "Alternando o teclado touch nativo do Ubuntu…";
    setRemoteToolbarExpanded(false);
  } catch (error) {
    if (!options.automatic) toast("Não foi possível abrir o teclado touch do Ubuntu nesta transmissão.", "error");
    throw error;
  }
}

function scheduleRemoteUbuntuKeyboardAutoHide(x, y, target) {
  if (String(target || "") !== "playwright") return;
  const remoteX = Number(x);
  const remoteY = Number(y);
  if (!Number.isFinite(remoteX) || !Number.isFinite(remoteY)) return;
  clearTimeout(state.remote.keyboardAutoHideTimer);
  state.remote.keyboardAutoHideTimer = setTimeout(async () => {
    state.remote.keyboardAutoHideTimer = null;
    try {
      const status = await api(`/api/remote-desktop/keyboard/auto-hide?target=playwright&x=${encodeURIComponent(remoteX)}&y=${encodeURIComponent(remoteY)}`, {method:"POST"});
      setRemoteKeyboardMode(status.visible ? "ubuntu" : "none");
      if (status.changed && selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = "Teclado touch do Ubuntu recolhido.";
    } catch (error) {
      console.debug("Não foi possível sincronizar o recolhimento do teclado Ubuntu", error);
    }
  }, 180);
}

function forwardRemoteKeyboardText(text) {
  if (!text) return;
  if (postNativeRemoteKeyboard({text})) return;
  const doc = remoteKeyboardDocument();
  const input = remoteKeyboardInput();
  if (input) {
    input.removeAttribute("readonly");
    input.value = `${input.value || ""}${text}`;
    input.dispatchEvent(new InputEvent("input", {bubbles:true, inputType:"insertText", data:text}));
    return;
  }
  const target = doc?.activeElement || doc?.body;
  for (const key of [...text]) {
    target?.dispatchEvent(new KeyboardEvent("keydown", {key, bubbles:true}));
    target?.dispatchEvent(new KeyboardEvent("keypress", {key, bubbles:true}));
    target?.dispatchEvent(new KeyboardEvent("keyup", {key, bubbles:true}));
  }
}

async function pasteRemoteDeviceClipboard() {
  if (!navigator.clipboard?.readText) {
    throw new Error("Este navegador não permite ler a área de transferência do aparelho.");
  }
  try {
    const text = await navigator.clipboard.readText();
    if (!text) {
      toast("A área de transferência do aparelho está vazia.");
      return;
    }
    forwardRemoteKeyboardText(text);
    if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = "Conteúdo do aparelho colado na janela remota.";
    setRemoteToolbarExpanded(false);
    toast("Conteúdo do aparelho colado.", "success");
  } catch (error) {
    const message = error?.name === "NotAllowedError"
      ? "Permita a colagem da área de transferência quando o Android solicitar."
      : (error?.message || "Não foi possível colar do aparelho.");
    toast(message, "error");
    throw error;
  }
}

function forwardRemoteKeyboardKey(key, code = key) {
  if (postNativeRemoteKeyboard({key, code})) return;
  const doc = remoteKeyboardDocument();
  const target = remoteKeyboardInput() || doc?.activeElement || doc?.body;
  if (!target) return;
  target.dispatchEvent(new KeyboardEvent("keydown", {key, code, bubbles:true}));
  target.dispatchEvent(new KeyboardEvent("keyup", {key, code, bubbles:true}));
}

function clearRemoteKeyboardCompositionTimer() {
  clearTimeout(state.remote.keyboardCompositionTimer);
  state.remote.keyboardCompositionTimer = null;
}

function commitRemoteKeyboardText(text) {
  const value = String(text || "");
  selectors.remoteKeyboardProxy.value = "";
  if (value) forwardRemoteKeyboardText(value);
}

function scheduleRemoteKeyboardCompositionCommit(text) {
  clearRemoteKeyboardCompositionTimer();
  state.remote.keyboardCompositionText = String(text || selectors.remoteKeyboardProxy.value || "");
  state.remote.keyboardCompositionTimer = setTimeout(() => {
    state.remote.keyboardCompositionTimer = null;
    const value = state.remote.keyboardCompositionText;
    state.remote.keyboardCompositionText = "";
    commitRemoteKeyboardText(value);
  }, 40);
}

function startRemoteKeyboardComposition() {
  clearRemoteKeyboardCompositionTimer();
  state.remote.keyboardComposing = true;
  state.remote.keyboardCompositionText = "";
}

function endRemoteKeyboardComposition(event) {
  state.remote.keyboardComposing = false;
  scheduleRemoteKeyboardCompositionCommit(event.data || selectors.remoteKeyboardProxy.value);
}

function handleRemoteKeyboardInput(event) {
  if (state.remote.keyboardComposing || event.isComposing) return;
  const inputType = String(event.inputType || "");
  if (state.remote.keyboardCompositionTimer) {
    const composed = event.data || selectors.remoteKeyboardProxy.value || state.remote.keyboardCompositionText;
    clearRemoteKeyboardCompositionTimer();
    state.remote.keyboardCompositionText = "";
    commitRemoteKeyboardText(composed);
    return;
  }
  if (inputType === "deleteContentBackward") {
    selectors.remoteKeyboardProxy.value = "";
    forwardRemoteKeyboardKey("Backspace", "Backspace");
    return;
  }
  if (inputType === "deleteContentForward") {
    selectors.remoteKeyboardProxy.value = "";
    forwardRemoteKeyboardKey("Delete", "Delete");
    return;
  }
  if (inputType === "insertLineBreak" || inputType === "insertParagraph") {
    selectors.remoteKeyboardProxy.value = "";
    forwardRemoteKeyboardKey("Enter", "Enter");
    return;
  }
  const text = event.data ?? selectors.remoteKeyboardProxy.value;
  commitRemoteKeyboardText(text);
}

function setRemoteKeyboardMode(mode) {
  state.remote.keyboardMode = ["android", "ubuntu"].includes(mode) ? mode : "none";
  el("remote-keyboard")?.setAttribute("aria-pressed", String(state.remote.keyboardMode === "android"));
  el("remote-keyboard-ubuntu")?.setAttribute("aria-pressed", String(state.remote.keyboardMode === "ubuntu"));
}

function suppressAndroidKeyboard() {
  const proxy = selectors.remoteKeyboardProxy;
  if (!proxy) return;
  proxy.blur();
  proxy.readOnly = true;
  proxy.setAttribute("inputmode", "none");
  try { navigator.virtualKeyboard?.hide(); } catch { /* Android variants may not expose the API. */ }
}

function hideRemoteKeyboard(options = {}) {
  clearRemoteKeyboardCompositionTimer();
  state.remote.keyboardComposing = false;
  state.remote.keyboardCompositionText = "";
  selectors.remoteKeyboardPanel.hidden = true;
  selectors.remoteKeyboardPanel.classList.remove("direct-input");
  suppressAndroidKeyboard();
  if (!options.preserveMode) setRemoteKeyboardMode("none");
}

function toggleRemoteAndroidKeyboard() {
  if (selectors.remoteKeyboardPanel.hidden || selectors.remoteKeyboardPanel.classList.contains("direct-input")) openRemoteKeyboard();
  else hideRemoteKeyboard();
}

function forwardRemoteWindowShortcut(key, code) {
  const doc = remoteKeyboardDocument();
  const target = remoteKeyboardInput() || doc?.activeElement || doc?.body;
  if (!target) return false;
  target.dispatchEvent(new KeyboardEvent("keydown", {key:"Alt", code:"AltLeft", bubbles:true, cancelable:true, altKey:true}));
  target.dispatchEvent(new KeyboardEvent("keydown", {key, code, bubbles:true, cancelable:true, altKey:true}));
  target.dispatchEvent(new KeyboardEvent("keyup", {key, code, bubbles:true, cancelable:true, altKey:true}));
  target.dispatchEvent(new KeyboardEvent("keyup", {key:"Alt", code:"AltLeft", bubbles:true, cancelable:true}));
  return true;
}

function sendRemoteWindowAction(action) {
  if (!state.remote.dialogOpen || !selectors.remoteFrame?.contentWindow) return;
  const shortcuts = {switch:["Tab", "Tab"], minimize:["F9", "F9"], maximize:["F10", "F10"], close:["F4", "F4"]};
  let nativeViewer = false;
  try { nativeViewer = selectors.remoteFrame.contentWindow.location.pathname === "/remote-viewer.html"; } catch { /* mesma origem é esperada, mas o fallback abaixo permanece seguro */ }
  if (nativeViewer) selectors.remoteFrame.contentWindow.postMessage({type:"sasocq-remote-window", action}, location.origin);
  else if (shortcuts[action]) forwardRemoteWindowShortcut(...shortcuts[action]);
  const labels = {switch:"Exibindo e trocando as janelas abertas…", minimize:"Minimizando a janela ativa…", maximize:"Expandindo ou restaurando a janela ativa…", close:"Fechando a janela ativa…"};
  if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = labels[action] || "Comando enviado à janela ativa.";
  setRemoteToolbarExpanded(false);
}

function renderRemoteGameControl() {
  const available = (state.remote.sessionTarget || state.remote.target || "codex") === "jogos";
  for (const id of ["remote-return-game", "remote-return-game-primary"]) {
    const button = el(id);
    if (button) button.hidden = !available;
  }
  const dock = el("remote-game-console-dock");
  if (dock) dock.hidden = !available;
}

async function returnToOpenGame() {
  const buttons = [el("remote-return-game"), el("remote-return-game-primary")].filter(Boolean);
  if ((state.remote.sessionTarget || state.remote.target) !== "jogos") return;
  buttons.forEach(button => { button.disabled = true; });
  if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = "Localizando o jogo que já está aberto…";
  try {
    const response = await api("/api/control/action", {
      method:"POST",
      body:JSON.stringify({action:"steam", params:{operation:"focus-game"}, confirmation:""}),
    });
    const result = controlOutput(response);
    const message = result.message || "Jogo aberto restaurado em primeiro plano.";
    if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = message;
    toast(message, "success");
  } catch (error) {
    if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = error.message;
    toast(error.message, "error");
  } finally {
    buttons.forEach(button => { button.disabled = false; });
    setRemoteToolbarExpanded(false);
  }
}

async function openRemoteSteamInterface(uiAction) {
  if ((state.remote.sessionTarget || state.remote.target) !== "jogos") return;
  const controls = {
    "main-menu": {button:el("remote-steam-menu"), pending:"Abrindo o menu STEAM nativo…"},
    "quick-access": {button:el("remote-steam-quick"), pending:"Abrindo o Acesso rápido do Steam Deck…"},
    back: {button:el("remote-steam-back"), pending:"Voltando uma tela no Steam…"},
  };
  const control = controls[uiAction];
  if (!control) return;
  control.button && (control.button.disabled = true);
  if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = control.pending;
  try {
    const response = await api("/api/control/action", {
      method:"POST",
      body:JSON.stringify({action:"steam", params:{operation:"ui-action", ui_action:uiAction}, confirmation:""}),
    });
    const result = controlOutput(response);
    const message = result.message || "Interface do Steam aberta.";
    if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = message;
    toast(message, "success");
  } catch (error) {
    if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = error.message;
    toast(error.message, "error");
  } finally {
    control.button && (control.button.disabled = false);
  }
}

function setRemoteToolbarExpanded(expanded) {
  const toolbar = el("remote-toolbar");
  const menu = el("remote-toolbar-menu");
  const toggle = el("remote-toolbar-toggle");
  const open = Boolean(expanded);
  toolbar?.classList.toggle("expanded", open);
  toolbar?.classList.toggle("collapsed", !open);
  if (menu) menu.hidden = !open;
  toggle?.setAttribute("aria-expanded", String(open));
  toggle?.setAttribute("aria-label", open ? "Ocultar controles da transmissão" : "Mostrar controles da transmissão");
  toggle?.setAttribute("title", open ? "Ocultar controles" : "Mostrar controles");
}

function remoteBrowserLayoutAvailable() {
  return (state.remote.sessionTarget || state.remote.target || "codex") === "playwright";
}

function renderRemoteMobileLayoutControl(mobile = state.remote.mobileLayout) {
  const button = el("remote-mobile-layout");
  if (!button) return;
  const available = remoteBrowserLayoutAvailable();
  state.remote.mobileLayout = Boolean(mobile && available);
  button.hidden = false;
  button.disabled = state.remote.mobileLayoutPending;
  button.setAttribute("aria-pressed", String(state.remote.mobileLayout));
  if (!available) {
    button.setAttribute("aria-label", "Abrir o navegador gerenciado em layout de celular");
    button.setAttribute("title", "Abrir navegador em layout de celular");
    const label = button.querySelector("span:last-child");
    if (label) label.textContent = "Abrir celular";
    return;
  }
  button.setAttribute("aria-label", state.remote.mobileLayout
    ? "Voltar a página aberta ao layout de computador"
    : "Usar layout de celular na página aberta");
  button.setAttribute("title", state.remote.mobileLayout ? "Voltar ao layout normal" : "Usar layout de celular");
  const label = button.querySelector("span:last-child");
  if (label) label.textContent = state.remote.mobileLayout ? "Layout normal" : "Layout celular";
}

async function syncRemoteMobileLayoutControl() {
  if (!remoteBrowserLayoutAvailable()) {
    renderRemoteMobileLayoutControl(false);
    return;
  }
  try {
    const status = await api("/api/remote-desktop/browser-layout");
    renderRemoteMobileLayoutControl(Boolean(status.mobile));
  } catch {
    renderRemoteMobileLayoutControl(false);
  }
}

async function toggleRemoteMobileLayout() {
  if (state.remote.mobileLayoutPending) return;
  const openManagedBrowser = !remoteBrowserLayoutAvailable();
  const desired = openManagedBrowser || !state.remote.mobileLayout;
  state.remote.mobileLayoutPending = true;
  renderRemoteMobileLayoutControl(state.remote.mobileLayout);
  try {
    if (openManagedBrowser) {
      if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = "Abrindo o navegador gerenciado em layout de celular…";
      await openRemoteDesktop({
        liveBrowser:true,
        threadId:String(state.activeThreadId || ""),
        preserveMobileLayoutPending:true,
      });
      if (state.remote.status?.target !== "playwright") {
        throw new Error("Não foi possível abrir o navegador gerenciado nesta transmissão.");
      }
      state.remote.mobileLayoutPending = true;
      renderRemoteMobileLayoutControl(state.remote.mobileLayout);
    }
    const status = await api(`/api/remote-desktop/browser-layout?mobile=${desired}`, {method:"POST"});
    renderRemoteMobileLayoutControl(Boolean(status.mobile));
    if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = status.mobile
      ? "Página aberta em layout de celular (390 × 844)."
      : "Página aberta em layout normal de computador.";
    toast(status.mobile ? "Layout de celular ativado na página." : "Layout normal restaurado.", "success");
  } catch (error) {
    renderRemoteMobileLayoutControl(state.remote.mobileLayout);
    toast(error.message, "error");
  } finally {
    state.remote.mobileLayoutPending = false;
    renderRemoteMobileLayoutControl(state.remote.mobileLayout);
  }
}

function clampRemoteViewport() {
  const stage = el("remote-stage");
  const zoom = Math.max(1, Math.min(Number(state.remote.viewportZoom || 1), 4));
  const maxX = Math.max(0, (stage?.clientWidth || 0) * (zoom - 1) / 2);
  const maxY = Math.max(0, (stage?.clientHeight || 0) * (zoom - 1) / 2);
  state.remote.viewportZoom = zoom;
  state.remote.viewportPanX = Math.max(-maxX, Math.min(maxX, Number(state.remote.viewportPanX || 0)));
  state.remote.viewportPanY = Math.max(-maxY, Math.min(maxY, Number(state.remote.viewportPanY || 0)));
}

function applyRemoteViewportTransform() {
  clampRemoteViewport();
  const frame = selectors.remoteFrame;
  if (!frame) return;
  frame.style.transform = `translate3d(${state.remote.viewportPanX}px, ${state.remote.viewportPanY}px, 0) scale(${state.remote.viewportZoom})`;
  el("remote-zoom-reset")?.classList.toggle("active", state.remote.viewportZoom > 1.01);
  el("remote-zoom-reset")?.setAttribute("aria-label", state.remote.viewportZoom > 1.01
    ? `Restaurar ampliação, atual ${Math.round(state.remote.viewportZoom * 100)}%`
    : "Restaurar ampliação da transmissão");
}

function resetRemoteViewport(announce = true) {
  state.remote.viewportZoom = 1;
  state.remote.viewportPanX = 0;
  state.remote.viewportPanY = 0;
  applyRemoteViewportTransform();
  if (announce) toast("Ampliação restaurada para 100%.", "success");
}

function openRemoteKeyboard(options = {}) {
  const direct = Boolean(options.direct);
  try {
    for (const doc of remoteViewerDocuments()) {
      const buttons = [...doc.querySelectorAll("button, [role='button']")];
      const button = doc.querySelector("#noVNC_keyboard_button, [data-i18n='Show Keyboard'], button[title*='keyboard' i]")
        || buttons.find(item => /teclado|keyboard|entrada de texto|text input/i.test(`${item.textContent || ""} ${item.title || ""} ${item.getAttribute("aria-label") || ""}`));
      if (button && !options.automatic) button.click();
    }
    if (state.remote.keyboardMode === "ubuntu") {
      const target = state.remote.sessionTarget || state.remote.target || "codex";
      if (target === "playwright") api("/api/remote-desktop/keyboard?target=playwright&visible=false", {method:"POST"}).catch(() => null);
    }
    setRemoteKeyboardMode("android");
    selectors.remoteKeyboardProxy.readOnly = false;
    selectors.remoteKeyboardProxy.setAttribute("inputmode", "text");
    selectors.remoteKeyboardPanel.hidden = false;
    selectors.remoteKeyboardPanel.classList.toggle("direct-input", direct);
    selectors.remoteKeyboardProxy.value = "";
    selectors.remoteKeyboardProxy.focus({preventScroll:true});
    if (!direct) selectors.remoteKeyboardProxy.click();
  } catch (error) {
    console.warn("Não foi possível acionar diretamente o teclado remoto", error);
    selectors.remoteKeyboardPanel.hidden = false;
    selectors.remoteKeyboardPanel.classList.toggle("direct-input", direct);
    selectors.remoteKeyboardProxy.readOnly = false;
    selectors.remoteKeyboardProxy.setAttribute("inputmode", "text");
    setRemoteKeyboardMode("android");
    selectors.remoteKeyboardProxy?.focus({preventScroll:true});
  }
}

function effectiveRemoteScale() {
  const selected = Number(selectors.remoteInterfaceScale?.value);
  if (Number.isFinite(selected) && selected >= 1) return selected;
  const stage = el("remote-stage");
  const shortest = Math.min(stage?.clientWidth || innerWidth, stage?.clientHeight || innerHeight);
  const touch = navigator.maxTouchPoints > 0 || matchMedia("(pointer: coarse)").matches;
  if (!touch) return 1;
  return shortest <= 600 ? 1.35 : shortest <= 1024 ? 1.2 : 1.1;
}

function applyRemoteInterfaceScale() {
  const scale = effectiveRemoteScale();
  el("remote-stage").style.setProperty("--remote-interface-scale", String(scale));
  state.remote.interfaceScale = selectors.remoteInterfaceScale?.value || "auto";
  return scale;
}

function ensureRemoteCursor() {
  let cursor = el("remote-cursor");
  if (!cursor) {
    cursor = document.createElement("span");
    cursor.id = "remote-cursor";
    cursor.className = "remote-cursor";
    cursor.hidden = true;
    el("remote-stage").append(cursor);
  }
  return cursor;
}

function updateRemoteCursor(doc, x, y) {
  const cursor = ensureRemoteCursor();
  const frameRect = selectors.remoteFrame.getBoundingClientRect();
  const stageRect = el("remote-stage").getBoundingClientRect();
  const scaleX = frameRect.width / Math.max(1, selectors.remoteFrame.clientWidth);
  const scaleY = frameRect.height / Math.max(1, selectors.remoteFrame.clientHeight);
  cursor.style.left = `${frameRect.left - stageRect.left + x * scaleX}px`;
  cursor.style.top = `${frameRect.top - stageRect.top + y * scaleY}px`;
  cursor.hidden = state.remote.pointerMode !== "trackpad";
}

function dispatchRemoteMouse(doc, type, x, y, buttons = 0) {
  const target = doc.elementFromPoint(x, y) || doc.body;
  target?.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, view:doc.defaultView, clientX:x, clientY:y, buttons, button:0}));
}

function sendRemoteScroll(dx, dy, announce = false) {
  const sent = postNativeRemotePointer({action:"scroll", dx:Number(dx) || 0, dy:Number(dy) || 0});
  if (announce && selectors.remoteLiveStatus) {
    selectors.remoteLiveStatus.textContent = sent ? "Rolagem enviada à tela remota." : "A rolagem remota ainda não está pronta.";
  }
  return sent;
}

function installRemoteTouchBridge() {
  installRemotePointerStateBridge();
  const coarse = navigator.maxTouchPoints > 0 || matchMedia("(pointer: coarse)").matches;
  for (const doc of remoteViewerDocuments()) {
    if (doc.documentElement.dataset.sasocqTouchBridge === "1") continue;
    doc.documentElement.dataset.sasocqTouchBridge = "1";
    const points = new Map();
    let gesture = null;
    let multiTouchActive = false;
    let nativeMultiTouchActive = false;
    const pointPair = () => [...points.values()].slice(0, 2);
    const touchPair = touches => [...touches].slice(0, 2).map(touch => ({x:touch.clientX, y:touch.clientY}));
    const distance = pair => Math.hypot(pair[1].x - pair[0].x, pair[1].y - pair[0].y);
    const center = pair => ({x:(pair[0].x + pair[1].x) / 2, y:(pair[0].y + pair[1].y) / 2});
    const beginMultiGesture = pair => {
      const startCenter = center(pair);
      gesture = {
        multi:true,
        mode:"pending",
        startDistance:Math.max(1, distance(pair)),
        startPoints:pair.map(point => ({...point})),
        startCenter,
        lastCenter:startCenter,
        startZoom:state.remote.viewportZoom,
        startPanX:state.remote.viewportPanX,
        startPanY:state.remote.viewportPanY,
      };
      multiTouchActive = true;
    };
    const updateMultiGesture = pair => {
      if (!gesture?.multi || pair.length < 2) return;
      const currentCenter = center(pair);
      const currentDistance = distance(pair);
      const radialDelta = Math.abs(currentDistance - gesture.startDistance);
      const centerDelta = Math.hypot(currentCenter.x - gesture.startCenter.x, currentCenter.y - gesture.startCenter.y);
      if (gesture.mode === "pending") {
        const movement = pair.map((point, index) => ({
          x:point.x - gesture.startPoints[index].x,
          y:point.y - gesture.startPoints[index].y,
        }));
        const magnitude = movement.map(vector => Math.hypot(vector.x, vector.y));
        if (Math.min(...magnitude) >= 3) {
          const alignment = (movement[0].x * movement[1].x + movement[0].y * movement[1].y)
            / Math.max(1, magnitude[0] * magnitude[1]);
          if (alignment >= .35) gesture.mode = "scroll";
          else if (radialDelta >= 8) gesture.mode = "zoom";
          else if (centerDelta >= 4) gesture.mode = "scroll";
        }
      }
      if (gesture.mode === "zoom") {
        state.remote.viewportZoom = Math.max(1, Math.min(4, gesture.startZoom * currentDistance / gesture.startDistance));
        state.remote.viewportPanX = gesture.startPanX + (currentCenter.x - gesture.startCenter.x) * state.remote.viewportZoom;
        state.remote.viewportPanY = gesture.startPanY + (currentCenter.y - gesture.startCenter.y) * state.remote.viewportZoom;
        applyRemoteViewportTransform();
      } else if (gesture.mode === "scroll") {
        const dx = currentCenter.x - gesture.lastCenter.x;
        const dy = currentCenter.y - gesture.lastCenter.y;
        sendRemoteScroll(-dx * 2.2, -dy * 2.2);
        gesture.lastCenter = currentCenter;
      }
    };
    doc.documentElement.style.touchAction = "none";
    doc.body && (doc.body.style.touchAction = "none");
    doc.addEventListener("touchstart", event => {
      if (event.touches.length < 2) return;
      if (gesture?.directDown) postNativeRemotePointer({action:"direct-cancel", x:gesture.lastX, y:gesture.lastY});
      nativeMultiTouchActive = true;
      beginMultiGesture(touchPair(event.touches));
      event.preventDefault();
      event.stopImmediatePropagation();
    }, {capture:true, passive:false});
    doc.addEventListener("touchmove", event => {
      if (!nativeMultiTouchActive) return;
      if (event.touches.length >= 2) updateMultiGesture(touchPair(event.touches));
      event.preventDefault();
      event.stopImmediatePropagation();
    }, {capture:true, passive:false});
    const finishNativeMultiTouch = event => {
      if (!nativeMultiTouchActive) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.touches.length >= 2) return;
      nativeMultiTouchActive = false;
      gesture = null;
      if (!event.touches.length && !points.size) multiTouchActive = false;
    };
    doc.addEventListener("touchend", finishNativeMultiTouch, {capture:true, passive:false});
    doc.addEventListener("touchcancel", finishNativeMultiTouch, {capture:true, passive:false});
    doc.addEventListener("pointerdown", event => {
      if (event.pointerType !== "touch") return;
      points.set(event.pointerId, {x:event.clientX, y:event.clientY});
      if (points.size >= 2) {
        if (gesture?.directDown) postNativeRemotePointer({action:"direct-cancel", x:gesture.lastX, y:gesture.lastY});
        beginMultiGesture(pointPair());
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      gesture = {multi:false, x:event.clientX, y:event.clientY, lastX:event.clientX, lastY:event.clientY, distance:0};
      if (state.remote.pointerMode !== "trackpad") {
        gesture.directDown = postNativeRemotePointer({action:"direct-down", x:event.clientX, y:event.clientY});
        if (!gesture.directDown) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      const bounds = doc.documentElement.getBoundingClientRect();
      if (!state.remote.cursorX || !state.remote.cursorY) {
        state.remote.cursorX = Math.max(1, bounds.width / 2);
        state.remote.cursorY = Math.max(1, bounds.height / 2);
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      updateRemoteCursor(doc, state.remote.cursorX, state.remote.cursorY);
    }, true);
    doc.addEventListener("pointermove", event => {
      if (event.pointerType !== "touch" || !points.has(event.pointerId)) return;
      points.set(event.pointerId, {x:event.clientX, y:event.clientY});
      if (nativeMultiTouchActive) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      if (points.size >= 2 && gesture?.multi) {
        updateMultiGesture(pointPair());
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      if (!gesture || gesture.multi) return;
      const dx = event.clientX - gesture.lastX;
      const dy = event.clientY - gesture.lastY;
      gesture.lastX = event.clientX;
      gesture.lastY = event.clientY;
      gesture.distance += Math.abs(dx) + Math.abs(dy);
      if (state.remote.pointerMode !== "trackpad") {
        if (!gesture.directDown) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        postNativeRemotePointer({action:"direct-move", x:event.clientX, y:event.clientY});
        return;
      }
      const width = doc.documentElement.clientWidth;
      const height = doc.documentElement.clientHeight;
      state.remote.cursorX = Math.max(1, Math.min(width - 2, state.remote.cursorX + dx * 1.25));
      state.remote.cursorY = Math.max(1, Math.min(height - 2, state.remote.cursorY + dy * 1.25));
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!postNativeRemotePointer({action:"move", dx:dx * 1.25, dy:dy * 1.25})) {
        dispatchRemoteMouse(doc, "mousemove", state.remote.cursorX, state.remote.cursorY);
      }
      updateRemoteCursor(doc, state.remote.cursorX, state.remote.cursorY);
    }, true);
    doc.addEventListener("pointerup", event => {
      if (event.pointerType !== "touch" || !points.has(event.pointerId)) return;
      if (nativeMultiTouchActive) {
        points.delete(event.pointerId);
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      const wasMultiTouch = Boolean(multiTouchActive || gesture?.multi || points.size > 1);
      points.delete(event.pointerId);
      if (wasMultiTouch) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!points.size) multiTouchActive = false;
        gesture = null;
        return;
      }
      if (!gesture) return;
      const moved = gesture.distance;
      if (state.remote.pointerMode === "trackpad") {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (moved < 8) {
          if (!postNativeRemotePointer({action:"click", button:"left"})) {
            dispatchRemoteMouse(doc, "mousedown", state.remote.cursorX, state.remote.cursorY, 1);
            dispatchRemoteMouse(doc, "mouseup", state.remote.cursorX, state.remote.cursorY, 0);
          }
        }
      } else if (gesture.directDown) {
        event.preventDefault();
        event.stopImmediatePropagation();
        postNativeRemotePointer({action:"direct-up", x:event.clientX, y:event.clientY});
      }
      gesture = null;
    }, true);
    doc.addEventListener("pointercancel", event => {
      if (gesture?.directDown) postNativeRemotePointer({action:"direct-cancel", x:gesture.lastX, y:gesture.lastY});
      points.delete(event.pointerId);
      if (nativeMultiTouchActive) return;
      if (!points.size) multiTouchActive = false;
      gesture = null;
    }, true);
  }
}

function installRemoteToolbarDrag() {
  const toolbar = el("remote-toolbar");
  const handle = el("remote-toolbar-drag");
  const stage = el("remote-stage");
  if (!toolbar || !handle || !stage || handle.dataset.dragReady === "1") return;
  handle.dataset.dragReady = "1";
  handle.addEventListener("pointerdown", event => {
    if (event.button !== undefined && event.button !== 0) return;
    const stageRect = stage.getBoundingClientRect();
    const rect = toolbar.getBoundingClientRect();
    state.remote.toolbarDrag = {
      pointerId:event.pointerId,
      startX:event.clientX,
      startY:event.clientY,
      left:rect.left - stageRect.left,
      top:rect.top - stageRect.top,
    };
    toolbar.style.left = `${state.remote.toolbarDrag.left}px`;
    toolbar.style.top = `${state.remote.toolbarDrag.top}px`;
    toolbar.style.right = "auto";
    toolbar.classList.add("dragging");
    handle.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  });
  handle.addEventListener("pointermove", event => {
    const drag = state.remote.toolbarDrag;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const maxLeft = Math.max(0, stage.clientWidth - toolbar.offsetWidth);
    const maxTop = Math.max(0, stage.clientHeight - toolbar.offsetHeight);
    toolbar.style.left = `${Math.max(0, Math.min(maxLeft, drag.left + event.clientX - drag.startX))}px`;
    toolbar.style.top = `${Math.max(0, Math.min(maxTop, drag.top + event.clientY - drag.startY))}px`;
    event.preventDefault();
  });
  const finish = event => {
    if (!state.remote.toolbarDrag || state.remote.toolbarDrag.pointerId !== event.pointerId) return;
    state.remote.toolbarDrag = null;
    toolbar.classList.remove("dragging");
    handle.releasePointerCapture?.(event.pointerId);
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
}

function refreshRemoteTouchBridge() {
  clearInterval(state.remote.bridgeTimer);
  installRemoteTouchBridge();
  let attempts = 0;
  state.remote.bridgeTimer = setInterval(() => {
    installRemoteTouchBridge();
    if (++attempts >= 20 || !state.remote.dialogOpen) {
      clearInterval(state.remote.bridgeTimer);
      state.remote.bridgeTimer = null;
    }
  }, 250);
}

function setRemotePointerMode(mode) {
  state.remote.pointerMode = mode === "trackpad" ? "trackpad" : "direct";
  if (selectors.remotePointerMode) selectors.remotePointerMode.value = state.remote.pointerMode;
  for (const button of [el("remote-pointer-direct"), el("remote-pointer-trackpad")]) {
    if (!button) continue;
    const selected = button.id === `remote-pointer-${state.remote.pointerMode}`;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  }
  el("remote-stage").classList.toggle("trackpad-mode", state.remote.pointerMode === "trackpad");
  ensureRemoteCursor().hidden = state.remote.pointerMode !== "trackpad";
  const help = el("remote-gesture-help");
  if (help) help.textContent = state.remote.pointerMode === "trackpad"
    ? "Um dedo move o ponteiro e um toque clica. Dois dedos rolam na vertical ou horizontal; afaste ou aproxime os dedos para ampliar."
    : "Um dedo toca diretamente na tela. Dois dedos rolam na vertical ou horizontal; afaste ou aproxime os dedos para ampliar.";
  try {
    selectors.remoteFrame?.contentWindow?.postMessage({type:"sasocq-remote-pointer-mode", mode:state.remote.pointerMode}, location.origin);
  } catch { /* O visor avisará quando estiver pronto. */ }
  if (selectors.remoteLiveStatus) selectors.remoteLiveStatus.textContent = state.remote.pointerMode === "trackpad"
    ? "Trackpad ativo • um dedo move • dois dedos rolam"
    : "Toque direto ativo • dois dedos rolam";
  refreshRemoteTouchBridge();
}

async function toggleRemoteFullscreen() {
  const stage = el("remote-stage");
  try {
    if (document.fullscreenElement) {
      el("remote-fullscreen").setAttribute("aria-pressed", "false");
      await document.exitFullscreen();
      await fitRemoteDesktop();
      return;
    }
    selectors.remoteDialog.classList.add("immersive");
    el("remote-fullscreen").setAttribute("aria-pressed", "true");
    const request = stage.requestFullscreen || stage.webkitRequestFullscreen || stage.msRequestFullscreen;
    if (request) await request.call(stage, {navigationUI:"hide"});
    await fitRemoteDesktop();
  } catch (error) {
    // Navegadores móveis podem recusar a API. O modo imersivo continua ativo
    // e mantém o Ubuntu ocupando toda a área útil do web app.
    selectors.remoteDialog.classList.add("immersive");
    await fitRemoteDesktop();
    toast("Painéis do Codex ocultos. Instale como app para ocultar também a barra do navegador.", "success");
  }
}

async function loadRateLimits(workspace = activeWorkspace(), options = {}) {
  try {
    const data = await api(`/api/account/rate-limits?${workspaceQuery(workspace)}`);
    const group = workspaceGroup(workspace);
    state.rateLimits[group] = data;
    if (String(workspace).startsWith("project:")) state.rateLimits.projects = data;
    const limit = data.rateLimits || Object.values(data.rateLimitsByLimitId || {})[0];
    if (options.announce && limit?.primary) {
      addActivity(`Uso do Codex • ${workspaceLabel(workspace)}`, `${quotaRemainingPercent(limit.primary)}% restante • renovação ${formatTime(limit.primary.resetsAt)}`);
      toast("Cotas do Codex atualizadas.", "success");
    }
    if (options.render !== false && state.lastStatus && !state.setup.active) renderSettings(state.lastStatus);
    renderHomeCodexUsage();
    return data;
  } catch (error) {
    state.rateLimits[workspaceGroup(workspace)] = null;
    if (options.announce) toast(`Não foi possível consultar as cotas: ${error.message}`, "error");
    if (options.render !== false && state.lastStatus && !state.setup.active) renderSettings(state.lastStatus);
    renderHomeCodexUsage();
    return null;
  }
}

function autoResizePrompt() {
  selectors.prompt.style.height = "auto";
  selectors.prompt.style.height = `${Math.min(selectors.prompt.scrollHeight, 180)}px`;
}

function closeMobilePanels() {
  selectors.sidebar.classList.remove("open");
  selectors.inspector.classList.remove("open");
}

const desktopLayout = window.matchMedia("(min-width: 901px)");

function setDesktopPanel(panel, open, {persist = true} = {}) {
  if (!desktopLayout.matches) {
    selectors[panel].classList.toggle("open", open);
    return;
  }
  const collapsedClass = `${panel}-collapsed`;
  el("app").classList.toggle(collapsedClass, !open);
  if (panel === "inspector") el("open-inspector").setAttribute("aria-pressed", String(open));
  if (persist) localStorage.setItem(`clc-desktop-${panel}`, open ? "open" : "closed");
}

function restoreDesktopPanels() {
  if (!desktopLayout.matches) return;
  setDesktopPanel("sidebar", localStorage.getItem("clc-desktop-sidebar") !== "closed", {persist:false});
  const inspectorPreference = localStorage.getItem("clc-desktop-inspector");
  const inspectorOpen = inspectorPreference ? inspectorPreference === "open" : window.innerWidth > 1450;
  setDesktopPanel("inspector", inspectorOpen, {persist:false});
}

function isMobileAppNavigation() {
  return /Android/i.test(navigator.userAgent || "")
    || window.matchMedia?.("(display-mode: standalone)")?.matches
    || window.navigator.standalone === true;
}

function handleAppBack() {
  if (selectors.taskDialog?.open) {
    if (!selectors.taskClose?.disabled) selectors.taskDialog.close();
    else toast("A operação atual precisa terminar antes de voltar.");
    return true;
  }
  if (state.remote.dialogOpen || selectors.remoteDialog?.open) { closeRemoteDialog(); return true; }
  if (selectors.pairingDialog?.open) { closePairingDialog(); return true; }
  if (selectors.toolsDialog?.open) { selectors.toolsDialog.close(); return true; }
  if (selectors.settingsDialog?.open) {
    if (state.settingsPage !== "overview") showSettingsPage("overview");
    else selectors.settingsDialog.close();
    return true;
  }
  if (selectors.loginDialog?.open) { selectors.loginDialog.close(); return true; }
  if (state.setup.active && state.setup.step > 0) { backSetup(); return true; }
  if (selectors.inspector?.classList.contains("open")) { selectors.inspector.classList.remove("open"); return true; }
  if (selectors.sidebar?.classList.contains("open")) { selectors.sidebar.classList.remove("open"); return true; }
  return false;
}

function installMobileBackNavigation() {
  if (!isMobileAppNavigation()) return;
  const cleanUrl = `${location.pathname}${location.search}${location.hash}`;
  history.replaceState({...history.state, clcAppRoot:true}, "", cleanUrl);
  history.pushState({clcAppGuard:true}, "", cleanUrl);
  window.addEventListener("popstate", () => {
    if (handleAppBack()) {
      window.setTimeout(() => history.pushState({clcAppGuard:true}, "", cleanUrl), 0);
      return;
    }
    history.back();
  });
}

function installVisualViewportSizing() {
  const root = document.documentElement;
  const sync = () => {
    const viewport = window.visualViewport;
    const height = Math.max(320, Math.round(viewport?.height || window.innerHeight));
    const offsetTop = Math.max(0, Math.round(viewport?.offsetTop || 0));
    const remoteOpen = document.body.classList.contains("remote-open");
    const stableRemoteHeight = Number(state.remote.stableViewportHeight || 0);
    const keyboardOpen = remoteOpen && stableRemoteHeight > 0
      ? stableRemoteHeight - height > 160
      : window.innerHeight - height > 160;
    const bottomInset = Math.max(0, Math.round(window.innerHeight - height - offsetTop));
    root.style.setProperty("--app-height", `${keyboardOpen ? stableRemoteHeight : height}px`);
    root.style.setProperty("--visual-viewport-bottom", `${bottomInset}px`);
    document.body.classList.toggle("virtual-keyboard-open", keyboardOpen);
  };
  sync();
  window.visualViewport?.addEventListener("resize", sync, {passive:true});
  window.visualViewport?.addEventListener("scroll", sync, {passive:true});
  window.addEventListener("orientationchange", sync, {passive:true});
  window.addEventListener("resize", sync, {passive:true});
}

function installComposerSizing() {
  const composer = document.querySelector(".composer-wrap");
  if (!composer || !("ResizeObserver" in window)) return;
  const sync = () => {
    document.documentElement.style.setProperty("--composer-height", `${Math.ceil(composer.getBoundingClientRect().height)}px`);
    if (state.activeThreadId && state.conversationAutoFollow) requestAnimationFrame(() => setConversationScrollTop(0));
  };
  new ResizeObserver(sync).observe(composer);
  sync();
}

function pushApplicationKey(value) {
  const padding = "=".repeat((4 - value.length % 4) % 4);
  const raw = atob((value + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, char => char.charCodeAt(0));
}

async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return null;
  const registration = await navigator.serviceWorker.register("/sw.js");
  if (window.Notification?.permission === "granted") ensurePushSubscription(false).catch(console.error);
  return registration;
}

async function ensurePushSubscription(promptForPermission = false) {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return false;
  let permission = Notification.permission;
  if (permission === "default" && promptForPermission) permission = await Notification.requestPermission();
  if (permission !== "granted") return false;
  const registration = await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    const data = await api("/api/push/public-key");
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly:true,
      applicationServerKey:pushApplicationKey(data.publicKey),
    });
  }
  const value = subscription.toJSON();
  await api("/api/push/subscriptions", {
    method:"POST",
    body:JSON.stringify({...value, name:browserDeviceName()}),
  });
  return true;
}

function bindEvents() {
  selectors.send.addEventListener("click", sendMessage);
  selectors.interrupt.addEventListener("click", interruptTurn);
  selectors.model.addEventListener("change", () => {
    state.composerPreferences.model = selectors.model.value;
    syncModelDependentControls();
    saveComposerPreferences();
  });
  selectors.effort.addEventListener("change", saveComposerPreferences);
  selectors.speed.addEventListener("change", saveComposerPreferences);
  selectors.network.addEventListener("change", saveComposerPreferences);
  selectors.prompt.addEventListener("input", autoResizePrompt);
  el("new-thread").addEventListener("click", newThread);
  el("project-new-thread").addEventListener("click", newThread);
  el("refresh-threads").addEventListener("click", loadThreads);
  el("thread-search").addEventListener("input", handleThreadSearchInput);
  selectors.messageList.addEventListener("click", event => {
    if (event.target.closest(".execution-status-card")) {
      resumeConversationAutoFollow();
      return;
    }
    const browserButton = event.target.closest("[data-browser-open]");
    if (browserButton) {
      openRemoteDesktop({target:"codex", liveBrowser:true, threadId:state.activeThreadId});
      return;
    }
    const androidButton = event.target.closest("[data-android-open]");
    if (androidButton) {
      openRemoteDesktop({liveAndroid:true});
      return;
    }
    if (!event.target.closest("[data-load-older-messages]")) return;
    state.messageRenderLimit += 100;
    renderMessages();
  });
  selectors.messageList.addEventListener("keydown", event => {
    if (!["Enter", " "].includes(event.key) || !event.target.closest(".execution-status-card")) return;
    event.preventDefault();
    resumeConversationAutoFollow();
  });
  el("add-project").addEventListener("click", () => openProjectManager("manage").catch(error => toast(error.message, "error")));
  el("create-project").addEventListener("click", () => openProjectManager("create").catch(error => toast(error.message, "error")));
  el("select-project-folder").addEventListener("click", () => openProjectManager("select").catch(error => toast(error.message, "error")));
  el("manage-projects").addEventListener("click", () => openProjectManager("manage").catch(error => toast(error.message, "error")));
  el("close-project-dialog").addEventListener("click", () => selectors.projectDialog.close());
  el("open-project-root-browser").addEventListener("click", () => openProjectRootBrowser().catch(error => toast(error.message, "error")));
  el("confirm-create-project").addEventListener("click", () => createProjectFolder().catch(error => toast(error.message, "error")));
  el("confirm-select-project").addEventListener("click", () => selectExistingProjectFolder().catch(error => toast(error.message, "error")));
  el("refresh-project-manager").addEventListener("click", () => refreshProjectManager().catch(error => toast(error.message, "error")));
  el("project-root-select").addEventListener("change", event => {
    const path = event.target.value;
    api("/api/projects/roots", {method:"POST", body:JSON.stringify({path})})
      .then(data => refreshProjectManager(data.root.path))
      .then(() => toast("Pasta padrão dos projetos atualizada.", "success"))
      .catch(error => toast(error.message, "error"));
  });
  el("project-folder-up").addEventListener("click", () => {
    if (state.projectFolderParent) refreshProjectManager(state.projectRoot, state.projectFolderParent).catch(error => toast(error.message, "error"));
  });
  selectors.projectFolderList.addEventListener("click", event => {
    const path = event.target.closest("[data-project-folder]")?.dataset.projectFolder;
    if (path) refreshProjectManager(state.projectRoot, path).catch(error => toast(error.message, "error"));
  });
  el("close-project-root-browser").addEventListener("click", () => selectors.projectRootDialog.close());
  el("project-root-browser-cancel").addEventListener("click", () => selectors.projectRootDialog.close());
  el("project-root-browser-refresh").addEventListener("click", () => loadProjectRootBrowser(state.projectRootBrowserPath).catch(error => toast(error.message, "error")));
  el("project-root-browser-up").addEventListener("click", () => loadProjectRootBrowser(state.projectRootBrowserParent).catch(error => toast(error.message, "error")));
  el("project-root-browser-create").addEventListener("click", () => createProjectRootBrowserFolder().catch(error => toast(error.message, "error")));
  el("project-root-browser-select").addEventListener("click", () => selectProjectRootBrowserFolder().catch(error => toast(error.message, "error")));
  selectors.projectRootBrowserList.addEventListener("click", event => {
    if (event.target.closest("[data-project-root-onedrive]")) {
      openOneDriveFolderBrowser("project-root").catch(error => toast(error.message, "error"));
      return;
    }
    const path = event.target.closest("[data-project-root-folder]")?.dataset.projectRootFolder;
    if (path) loadProjectRootBrowser(path).catch(error => toast(error.message, "error"));
  });
  el("add-project-folder-selection").addEventListener("click", () => {
    if (!state.projectFolderPath) return toast("Escolha uma pasta.", "error");
    state.projectSelectedPaths = [...new Set([...(state.projectSelectedPaths || []), state.projectFolderPath])];
    renderProjectManager();
  });
  el("project-folder-selection-list").addEventListener("click", event => {
    const remove = event.target.closest("[data-remove-project-folder]");
    if (!remove) return;
    state.projectSelectedPaths.splice(Number(remove.dataset.removeProjectFolder), 1);
    renderProjectManager();
  });
  selectors.projectManagerList.addEventListener("click", event => {
    const projectId = event.target.closest("[data-open-project]")?.dataset.openProject;
    if (!projectId) return;
    selectProject(projectId).then(() => selectors.projectDialog.close()).catch(error => toast(error.message, "error"));
  });
  el("rename-thread").addEventListener("click", renameActiveThread);
  el("archive-thread").addEventListener("click", archiveActiveThread);
  el("start-login").addEventListener("click", () => { closeMobilePanels(); startDeviceLogin(activeWorkspace()); });
  el("close-login").addEventListener("click", () => selectors.loginDialog.close());
  el("open-tools").addEventListener("click", () => { closeMobilePanels(); openToolsDialog(); });
  el("open-terminal").addEventListener("click", openTerminal);
  el("context-open-shell")?.addEventListener("click", openShellTerminal);
  el("close-terminal").addEventListener("click", () => { closeTerminalSocket(); selectors.terminalDialog.close(); });
  el("terminal-reconnect").addEventListener("click", () => connectTerminal());
  el("terminal-clear").addEventListener("click", () => { selectors.terminalOutput.textContent = ""; });
  el("terminal-form").addEventListener("submit", event => {
    event.preventDefault();
    const command = selectors.terminalInput.value;
    if (!command) return;
    if (state.terminalWorkspace === "codex-emergency") {
      if (!terminalSend(command)) return;
      selectors.terminalInput.value = "";
      window.setTimeout(() => terminalSend("\r"), 180);
      return;
    }
    if (terminalSend(`${command}\n`)) selectors.terminalInput.value = "";
  });
  document.querySelectorAll("[data-terminal-key]").forEach(button => button.addEventListener("click", () => {
    const keys = {"\\u0003":"\u0003", "\\u0004":"\u0004", "\\t":"\t"};
    terminalSend(keys[button.dataset.terminalKey] || button.dataset.terminalKey);
    selectors.terminalInput.focus();
  }));
  document.querySelectorAll("[data-terminal-command]").forEach(button => button.addEventListener("click", () => terminalSend(`${button.dataset.terminalCommand}\n`)));
  selectors.terminalDialog.addEventListener("cancel", () => closeTerminalSocket());
  el("close-backup-folder").addEventListener("click", () => selectors.backupFolderDialog.close());
  el("backup-folder-cancel").addEventListener("click", () => selectors.backupFolderDialog.close());
  el("backup-folder-refresh").addEventListener("click", () => loadBackupFolders(state.backupFolderPath).catch(error => toast(error.message, "error")));
  el("backup-folder-up").addEventListener("click", () => loadBackupFolders(state.backupFolderParent).catch(error => toast(error.message, "error")));
  el("backup-folder-create").addEventListener("click", () => createBackupFolder().catch(error => toast(error.message, "error")));
  el("backup-folder-select").addEventListener("click", () => selectBackupFolder().catch(error => toast(error.message, "error")));
  selectors.backupFolderList.addEventListener("click", event => {
    const path = event.target.closest("[data-backup-folder]")?.dataset.backupFolder;
    if (path) loadBackupFolders(path).catch(error => toast(error.message, "error"));
  });
  selectors.composerTools.addEventListener("click", openToolsDialog);
  selectors.homeCodexUsage?.addEventListener("click", event => {
    if (!event.target.closest('[data-settings-action="refresh-codex-usage"]')) return;
    loadRateLimits(activeWorkspace(), {announce:true}).catch(error => toast(error.message, "error"));
  });
  el("open-system").addEventListener("click", async () => {
    if (selectors.loginDialog.open) selectors.loginDialog.close();
    closeMobilePanels();
    state.settingsPage = "system-control";
    if (!selectors.settingsDialog.open) selectors.settingsDialog.showModal();
    await Promise.all([loadProjects(), loadStatus(), loadConfiguredAccounts()]);
  });
  const openPCData = () => {
    closeMobilePanels();
    loadPCResources({openDialog:true}).catch(error => toast(error.message, "error"));
  };
  el("open-pc-data")?.addEventListener("click", openPCData);
  el("resource-alert")?.addEventListener("click", openPCData);
  el("close-pc-data")?.addEventListener("click", () => selectors.pcDataDialog?.close());
  el("refresh-pc-data")?.addEventListener("click", () => loadPCResources({announce:true}));
  el("pc-data-system-details")?.addEventListener("click", () => {
    selectors.pcDataDialog?.close();
    el("open-system").click();
  });
  const openSettings = async () => {
    if (selectors.loginDialog.open) selectors.loginDialog.close();
    closeMobilePanels();
    state.settingsPage = "overview";
    if (!selectors.settingsDialog.open) selectors.settingsDialog.showModal();
    await Promise.all([loadProjects(), loadStatus(), loadConfiguredAccounts()]);
  };
  el("open-settings").addEventListener("click", openSettings);
  el("rail-open-settings")?.addEventListener("click", openSettings);
  el("close-settings").addEventListener("click", () => selectors.settingsDialog.close());
  el("close-settings-bottom").addEventListener("click", () => selectors.settingsDialog.close());
  el("refresh-status").addEventListener("click", async () => { await loadProjects(); await loadStatus(); await loadConfiguredAccounts(); });
  el("open-conversation-home").addEventListener("click", () => clearProjectSelection().catch(error => toast(error.message, "error")));
  el("reload-page").addEventListener("click", () => window.location.reload());
  el("system-update").addEventListener("click", () => handleSystemUpdate());
  el("system-update-blockers-close").addEventListener("click", () => el("system-update-blockers-dialog").close());
  el("system-update-progress-close").addEventListener("click", () => {
    state.systemUpdateProgressDismissed = true;
    el("system-update-dialog").close();
  });
  el("open-sidebar").addEventListener("click", () => setDesktopPanel("sidebar", true));
  el("close-sidebar").addEventListener("click", () => setDesktopPanel("sidebar", false));
  el("open-inspector").addEventListener("click", () => {
    const open = !desktopLayout.matches || el("app").classList.contains("inspector-collapsed");
    setDesktopPanel("inspector", open);
  });
  el("close-inspector").addEventListener("click", () => setDesktopPanel("inspector", false));

  selectors.setupBack.addEventListener("click", backSetup);
  selectors.setupNext.addEventListener("click", advanceSetup);
  selectors.setupRefresh.addEventListener("click", refreshSetupState);
  selectors.setupBody.addEventListener("change", event => {
    if (event.target.name === "remote-choice") {
      state.setup.remoteChoice = event.target.value;
      renderSetupWizard();
    }
    if (event.target.name === "cloud-choice") {
      state.setup.cloudChoice = event.target.value;
      state.setup.cloudChoiceTouched = true;
      renderSetupWizard();
    }
    if (event.target.id === "cloud-initial-strategy") state.setup.cloudStrategy = event.target.value;
    if (event.target.id === "setup-autostart") state.setup.startAtLogin = event.target.checked;
  });
  selectors.setupBody.addEventListener("click", event => {
    const action = event.target.closest("[data-setup-action]")?.dataset.setupAction;
    if (action) handleSetupAction(action);
  });

  el("close-tools").addEventListener("click", () => selectors.toolsDialog.close());
  el("refresh-tools").addEventListener("click", () => loadExtensions(true));
  el("save-tools").addEventListener("click", () => saveToolAssociation().catch(error => toast(error.message, "error")));
  document.querySelectorAll('input[name="tool-scope"]').forEach(input => input.addEventListener("change", () => { state.toolsScope = input.value; }));
  document.querySelectorAll(".tools-tab").forEach(button => button.addEventListener("click", () => { state.toolsTab = button.dataset.toolsTab; renderToolsDialog(); }));
  selectors.toolsContent.addEventListener("change", event => {
    const input = event.target.closest("[data-profile-toggle]");
    if (input) updateProfileSelection(input.dataset.profileToggle, input.dataset.profileToggle, input.checked);
    const selected = event.target.closest("[data-select-kind]");
    if (selected) updateProfileSelection(selected.dataset.selectKind, selected.dataset.selectValue, selected.checked);
  });
  selectors.toolsContent.addEventListener("input", event => {
    const search = event.target.closest("[data-plugin-search]");
    if (!search) return;
    state.pluginSearch = search.value;
    const selection = search.selectionStart;
    renderPluginTools(state.extensions || {});
    const replacement = selectors.toolsContent.querySelector("[data-plugin-search]");
    replacement?.focus();
    if (selection != null) replacement?.setSelectionRange(selection, selection);
  });
  selectors.toolsContent.addEventListener("click", event => {
    const tab = event.target.closest("[data-open-tools-tab]");
    if (tab) { state.toolsTab = tab.dataset.openToolsTab; renderToolsDialog(); return; }
    const toggle = event.target.closest("[data-extension-toggle]");
    if (toggle) { toggleExtension(toggle.dataset.extensionToggle, toggle).catch(error => toast(error.message, "error")); return; }
    const url = event.target.closest("[data-open-url]")?.dataset.openUrl;
    if (url) { window.open(url, "_blank", "noopener"); return; }
    const oauth = event.target.closest("[data-mcp-oauth]")?.dataset.mcpOauth;
    if (oauth) { startMcpOAuth(oauth).catch(error => toast(error.message, "error")); return; }
    const plugin = event.target.closest("[data-plugin-install]");
    if (plugin) { installCatalogPlugin(plugin).catch(error => toast(error.message, "error")); return; }
    if (event.target.closest("[data-add-custom-mcp]")) addCustomMcp().catch(error => toast(error.message, "error"));
  });

  el("open-remote-desktop").addEventListener("click", openRemoteEnvironmentChooser);
  el("open-remote-browser").addEventListener("click", () => openRemoteDesktop({browser:true}));
  el("remote-environment-close").addEventListener("click", closeRemoteEnvironmentChooser);
  selectors.remoteEnvironmentDialog.addEventListener("click", event => {
    const entry = event.target.closest("[data-remote-entry]")?.dataset.remoteEntry;
    if (entry) launchRemoteEnvironment(entry).catch(error => toast(error.message, "error"));
  });
  selectors.remoteEnvironmentDialog.addEventListener("cancel", event => { event.preventDefault(); closeRemoteEnvironmentChooser(); });
  el("remote-toolbar-toggle").addEventListener("click", () => setRemoteToolbarExpanded(!el("remote-toolbar").classList.contains("expanded")));
  el("remote-change-environment").addEventListener("click", () => { closeRemoteDialog(); openRemoteEnvironmentChooser(); });
  el("remote-close").addEventListener("click", closeRemoteDialog);
  el("remote-stop").addEventListener("click", stopRemoteDesktop);
  el("remote-launch-browser").addEventListener("click", () => launchRemoteApplication("browser"));
  el("remote-launch-files").addEventListener("click", () => launchRemoteApplication("files"));
  el("remote-mobile-layout").addEventListener("click", () => toggleRemoteMobileLayout());
  el("remote-return-game").addEventListener("click", () => returnToOpenGame());
  el("remote-return-game-primary").addEventListener("click", () => returnToOpenGame());
  el("remote-steam-menu").addEventListener("click", () => openRemoteSteamInterface("main-menu"));
  el("remote-steam-quick").addEventListener("click", () => openRemoteSteamInterface("quick-access"));
  el("remote-steam-back").addEventListener("click", () => openRemoteSteamInterface("back"));
  el("remote-keyboard").addEventListener("click", () => { setRemoteToolbarExpanded(false); toggleRemoteAndroidKeyboard(); });
  el("remote-keyboard-ubuntu").addEventListener("click", () => showRemoteUbuntuKeyboard().catch(error => toast(error.message, "error")));
  el("remote-paste-device").addEventListener("click", () => pasteRemoteDeviceClipboard().catch(() => null));
  el("remote-keyboard-immersive").addEventListener("click", openRemoteKeyboard);
  el("remote-keyboard-hide").addEventListener("click", hideRemoteKeyboard);
  el("remote-keyboard-enter").addEventListener("click", () => { forwardRemoteKeyboardKey("Enter", "Enter"); selectors.remoteKeyboardProxy.focus({preventScroll:true}); });
  el("remote-keyboard-backspace").addEventListener("click", () => { forwardRemoteKeyboardKey("Backspace", "Backspace"); selectors.remoteKeyboardProxy.focus({preventScroll:true}); });
  selectors.remoteKeyboardProxy.addEventListener("compositionstart", startRemoteKeyboardComposition);
  selectors.remoteKeyboardProxy.addEventListener("compositionend", endRemoteKeyboardComposition);
  selectors.remoteKeyboardProxy.addEventListener("input", handleRemoteKeyboardInput);
  selectors.remoteKeyboardProxy.addEventListener("blur", () => {
    if (!selectors.remoteKeyboardPanel.classList.contains("direct-input")) return;
    if (state.remote.keyboardComposing) {
      state.remote.keyboardComposing = false;
      scheduleRemoteKeyboardCompositionCommit(selectors.remoteKeyboardProxy.value);
    }
    // Android may briefly move focus while resizing the visual viewport or
    // accepting an IME suggestion. Keep the direct-input bridge armed; the
    // next tap can focus it again without losing the pending composition.
    if (state.remote.dialogOpen) return;
    selectors.remoteKeyboardPanel.hidden = true;
    selectors.remoteKeyboardPanel.classList.remove("direct-input");
    el("remote-keyboard").setAttribute("aria-pressed", "false");
  });
  selectors.remoteKeyboardProxy.addEventListener("keydown", event => {
    if (!["Enter", "Backspace", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    forwardRemoteKeyboardKey(event.key, event.code);
  });
  el("remote-fit").addEventListener("click", fitRemoteDesktop);
  el("remote-zoom-reset").addEventListener("click", () => resetRemoteViewport(true));
  el("remote-fullscreen").addEventListener("click", toggleRemoteFullscreen);
  el("remote-window-switch").addEventListener("click", () => sendRemoteWindowAction("switch"));
  el("remote-window-minimize").addEventListener("click", () => sendRemoteWindowAction("minimize"));
  el("remote-window-maximize").addEventListener("click", () => sendRemoteWindowAction("maximize"));
  el("remote-window-close").addEventListener("click", () => sendRemoteWindowAction("close"));
  el("remote-exit-immersive").addEventListener("click", toggleRemoteFullscreen);
  el("remote-close-immersive").addEventListener("click", closeRemoteDialog);
  document.addEventListener("fullscreenchange", () => {
    el("remote-fullscreen").setAttribute("aria-pressed", String(Boolean(document.fullscreenElement)));
    if (selectors.remoteDialog.classList.contains("immersive")) fitRemoteDesktop();
  });
  selectors.remoteTarget.addEventListener("change", async () => {
    state.remote.target = selectors.remoteTarget.value;
    renderRemoteGameControl();
    const physical = state.remote.target !== "codex";
    el("remote-launch-browser").disabled = physical;
    el("remote-launch-files").disabled = physical;
    selectors.remoteProfile.disabled = physical;
    if (state.remote.dialogOpen) await openRemoteDesktop({target:state.remote.target});
    else await refreshRemoteStatus();
  });
  selectors.remoteProfile.addEventListener("change", async () => {
    await fitRemoteDesktop();
    if (state.remote.status?.browser_mode) await launchRemoteApplication("browser");
  });
  selectors.remotePointerMode.addEventListener("change", () => setRemotePointerMode(selectors.remotePointerMode.value));
  el("remote-pointer-direct").addEventListener("click", () => setRemotePointerMode("direct"));
  el("remote-pointer-trackpad").addEventListener("click", () => setRemotePointerMode("trackpad"));
  el("remote-scroll-left")?.addEventListener("click", () => sendRemoteScroll(-160, 0, true));
  el("remote-scroll-up")?.addEventListener("click", () => sendRemoteScroll(0, -160, true));
  el("remote-scroll-down")?.addEventListener("click", () => sendRemoteScroll(0, 160, true));
  el("remote-scroll-right")?.addEventListener("click", () => sendRemoteScroll(160, 0, true));
  selectors.remoteInterfaceScale.addEventListener("change", async () => {
    applyRemoteInterfaceScale();
    await fitRemoteDesktop();
    refreshRemoteTouchBridge();
  });
  selectors.remoteFrame.addEventListener("load", () => {
    state.remote.frameReady = true;
    setRemotePlaceholder(false);
    applyRemoteInterfaceScale();
    setRemotePointerMode(selectors.remotePointerMode.value);
    refreshRemoteTouchBridge();
  });
  installRemoteToolbarDrag();
  setRemoteToolbarExpanded(false);
  selectors.remoteDialog.addEventListener("close", () => {
    state.remote.dialogOpen = false;
    document.body.classList.remove("remote-open");
  });
  selectors.remoteDialog.addEventListener("cancel", event => {
    event.preventDefault();
    closeRemoteDialog();
  });

  el("retry-device-auth").addEventListener("click", async () => {
    const session = await establishSession().catch(error => { setDeviceAuthOverlay(true, error.message); return null; });
    if (session) { state.session = session; state.csrf = session.csrf; state.identity = session.identity; location.reload(); }
  });
  el("close-pairing").addEventListener("click", () => closePairingDialog());
  el("pairing-done").addEventListener("click", () => closePairingDialog());
  el("copy-pairing-link").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(selectors.pairingLink.href); toast("Endereço copiado.", "success"); }
    catch { window.prompt("Copie o endereço:", selectors.pairingLink.href); }
  });

  selectors.taskDone.addEventListener("click", () => selectors.taskDialog.close());
  selectors.taskClose.addEventListener("click", () => { if (!selectors.taskClose.disabled) selectors.taskDialog.close(); });
  selectors.taskActionLink.addEventListener("click", event => {
    const url = selectors.taskActionLink.href;
    if (!isLoopbackAuthorizationUrl(url)) return;
    event.preventDefault();
    if (selectors.taskDialog.open) selectors.taskDialog.close();
    if (selectors.settingsDialog.open) selectors.settingsDialog.close();
    openRemoteDesktop({target:"codex", browser:true, url});
  });
  selectors.settingsContent.addEventListener("click", event => {
    if (event.target.closest("[data-settings-home]")) { showSettingsPage("overview"); return; }
    const targetId = event.target.closest("[data-settings-jump]")?.dataset.settingsJump;
    if (targetId) {
      showSettingsPage(targetId);
      return;
    }
    const action = event.target.closest("[data-settings-action]")?.dataset.settingsAction;
    if (action) { handleSettingsAction(action).catch(error => toast(error.message, "error")); return; }
    const deviceId = event.target.closest("[data-device-revoke]")?.dataset.deviceRevoke;
    if (deviceId) revokePairedDevice(deviceId).catch(error => toast(error.message, "error"));
    const approveId = event.target.closest("[data-enrollment-approve]")?.dataset.enrollmentApprove;
    if (approveId) decideEnrollmentRequest(approveId, "approve").catch(error => toast(error.message, "error"));
    const rejectId = event.target.closest("[data-enrollment-reject]")?.dataset.enrollmentReject;
    if (rejectId) decideEnrollmentRequest(rejectId, "reject").catch(error => toast(error.message, "error"));
  });
  selectors.settingsContent.addEventListener("change", event => {
    if (event.target.id === "settings-system-update-automatic") {
      const enabled = event.target.checked;
      setAutomaticSystemUpdates(enabled)
        .catch(error => { event.target.checked = !enabled; toast(error.message, "error"); });
    }
    if (event.target.id === "settings-autostart") {
      api("/api/system/autostart", {method:"POST", body:JSON.stringify({enabled:event.target.checked})})
        .then(() => toast("Inicialização automática atualizada.", "success"))
        .then(loadStatus)
        .catch(error => { event.target.checked = !event.target.checked; toast(error.message, "error"); });
    }
    if (event.target.id === "settings-cloud-interval") {
      api("/api/cloud/timer", {method:"POST", body:JSON.stringify({enabled:true, interval_minutes:Number(event.target.value)})})
        .then(() => toast("Intervalo da nuvem atualizado.", "success"))
        .then(loadStatus)
        .catch(error => toast(error.message, "error"));
    }
    if (event.target.id === "settings-cloud-filter") {
      api("/api/cloud/filter", {method:"POST", body:JSON.stringify({profile:event.target.value})})
        .then(() => toast("Perfil de sincronização atualizado.", "success"))
        .then(loadStatus)
        .catch(error => toast(error.message, "error"));
    }
    if (event.target.matches("[data-worker-priority]")) {
      const projectId = event.target.dataset.workerPriority;
      sendControl("workers", {operation:"priority", project_id:projectId, priority:event.target.value})
        .then(() => toast("Prioridade do projeto atualizada.", "success"))
        .catch(error => toast(error.message, "error"));
    }
  });

  document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === button));
    document.querySelectorAll(".tab-panel").forEach(panel => panel.classList.toggle("active", panel.id === `tab-${button.dataset.tab}`));
    if (button.dataset.tab === "terminal") openTerminal();
    if (button.dataset.tab === "screen") refreshRemoteStatus();
  }));
  document.querySelectorAll(".suggestion").forEach(button => button.addEventListener("click", () => {
    if (!state.activeProject) return toast("Selecione um projeto para usar esta opção.");
    selectors.prompt.value = button.dataset.prompt; autoResizePrompt(); selectors.prompt.focus();
  }));
  document.addEventListener("click", event => {
    if (state.projectMenu && !event.target.closest(".floating-row-menu") && !event.target.closest(".project-icon-menu")) closeProjectMenu();
    const approvalOrigin = event.target.closest("[data-open-approval-origin]");
    if (approvalOrigin) {
      openApprovalOrigin(approvalOrigin.dataset.openApprovalOrigin).catch(error => toast(error.message, "error"));
      return;
    }
    const approval = event.target.closest("[data-approval]");
    if (approval) respondApproval(approval.dataset.approval, approval.dataset.action);
    const rename = event.target.closest("[data-project-rename]");
    if (rename) renameProject(rename.dataset.projectRename);
    const remove = event.target.closest("[data-project-delete]");
    if (remove) deleteProjectGraphically(remove.dataset.projectDelete);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeProjectMenu();
  });
  const remoteResizeTarget = window.visualViewport || window;
  remoteResizeTarget.addEventListener("resize", fitRemoteDesktop, {passive:true});
  window.addEventListener("orientationchange", () => {
    window.setTimeout(() => {
      if (state.remote.dialogOpen) captureRemoteStableViewport();
      fitRemoteDesktop();
    }, 250);
  }, {passive:true});

  window.addEventListener("beforeinstallprompt", event => {
    event.preventDefault(); state.deferredInstall = event; el("install-pwa").classList.remove("hidden");
  });
  el("install-pwa").addEventListener("click", async () => {
    if (state.deferredInstall) { await state.deferredInstall.prompt(); state.deferredInstall = null; el("install-pwa").classList.add("hidden"); }
  });
  if ("Notification" in window && Notification.permission === "default") {
    document.addEventListener("click", () => ensurePushSubscription(true).catch(console.error), {once:true});
  } else if (window.Notification?.permission === "granted") {
    ensurePushSubscription(false).catch(console.error);
  }
  navigator.serviceWorker?.addEventListener("message", event => {
    if (event.data?.kind === "push-completion" && document.visibilityState === "visible") {
      toast(event.data.payload?.title || "Atividade concluída.", "success");
    }
    if (event.data?.kind === "open-notification-target") {
      state.pendingNotificationTarget = event.data.payload || null;
      void openNotificationTarget().catch(error => toast(`Não foi possível abrir a conversa da notificação: ${error.message}`, "error"));
    }
  });
}

bindEvents();
restoreDesktopPanels();
desktopLayout.addEventListener?.("change", restoreDesktopPanels);
installVisualViewportSizing();
installConversationScrollControl();
installComposerSizing();
installMobileBackNavigation();
bootstrap();
