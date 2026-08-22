/* Codex Operations workspace: durable queue, navigation metadata and @ context. */
(() => {
  Object.assign(state, {
    queue: [],
    references: [],
    chatgptReferences: [],
    chatgptReferenceLoading: false,
    chatgptReferenceQuery: "",
    archivedThreads: false,
    operationMetadata: {threads:{}, projects:{}, chrome:{connected:false}},
  });

  const byId = id => document.getElementById(id);
  const escape = value => escapeHTML(String(value ?? ""));

  function payloadReferences() {
    return (state.references || []).map(reference => {
      if (reference.type === "chatgpt") return {type:"chatgpt-title", name:reference.name};
      return reference;
    });
  }

  function activePayload(message) {
    return {
      project_id: state.activeProject?.id,
      message,
      model: selectors.model.value || null,
      effort: selectors.effort.value || null,
      service_tier: selectors.speed.value || null,
      network_access: selectors.network.value === "enabled",
      tools: state.toolProfile,
      references: payloadReferences(),
      collaboration_mode: state.composerMode === "plan" ? "plan" : null,
      goal_mode: state.composerMode === "goal",
    };
  }

  function setInspectorTab(name) {
    document.querySelectorAll(".inspector .tab").forEach(tab => tab.classList.toggle("active", tab.dataset.tab === name));
    document.querySelectorAll(".inspector .tab-panel").forEach(panel => panel.classList.toggle("active", panel.id === `tab-${name}`));
    setDesktopPanel("inspector", true);
  }

  async function loadOperationMetadata() {
    try { state.operationMetadata = await api("/api/navigation/metadata"); }
    catch (error) { console.warn("Metadados de navegação indisponíveis", error); }
  }

  async function loadOperationQueue() {
    const threadId = state.activeThreadId;
    if (!threadId) {
      state.queue = [];
      renderOperationQueue();
      return;
    }
    try {
      const data = await api(`/api/threads/${encodeURIComponent(threadId)}/queue`);
      if (state.activeThreadId !== threadId) return;
      state.queue = data.items || [];
    } catch (error) {
      if (state.activeThreadId !== threadId) return;
      state.queue = [];
      toast(error.message, "error");
    }
    renderOperationQueue();
  }

  function queueStatusLabel(status) {
    return ({queued:"Na fila", steering:"Enviando orientação", steered:"Orientação enviada", running:"Executando", completed:"Concluído", cancelled:"Cancelado", failed:"Falhou"})[status] || status;
  }

  function renderOperationQueue() {
    const list = byId("queue-list");
    const contextualQueue = state.activeThreadId ? state.queue : [];
    const pending = contextualQueue.filter(item => ["queued", "steering", "running"].includes(item.status));
    ["queue-count", "rail-queue-count", "mobile-queue-count"].forEach(id => { if (byId(id)) byId(id).textContent = pending.length; });
    renderComposerQueue(pending);
    if (!list) return;
    if (!state.activeThreadId) {
      list.innerHTML = '<div class="panel-empty">Abra uma conversa para consultar a fila.</div>';
      return;
    }
    if (!state.queue.length) {
      list.innerHTML = '<div class="panel-empty">A fila desta conversa está vazia.</div>';
      return;
    }
    list.innerHTML = state.queue.map((item, index) => `
      <article class="queue-card ${escape(item.status)}" draggable="${item.status === "queued"}" data-queue-id="${escape(item.id)}">
        <div class="queue-card-head"><span class="queue-index">${index + 1}</span><strong>${escape(queueStatusLabel(item.status))}</strong><span>${item.created_at ? new Date(item.created_at * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}) : ""}</span></div>
        <p>${escape(item.message)}</p>
        ${item.references?.length ? `<small>${item.references.length} referência(s) anexada(s)</small>` : ""}
        ${item.status === "queued" ? `<div class="queue-actions">${state.activeTurnId ? `<button class="secondary-button" data-queue-steer="${escape(item.id)}" title="Enviar esta direção ao turno em execução">Orientar agora</button>` : ""}<button class="ghost-button" data-queue-up="${escape(item.id)}" title="Mover para cima">↑</button><button class="ghost-button" data-queue-down="${escape(item.id)}" title="Mover para baixo">↓</button><button class="ghost-button" data-queue-edit="${escape(item.id)}">Editar</button><button class="danger-button" data-queue-delete="${escape(item.id)}">Excluir</button></div>` : ""}
      </article>`).join("");
  }

  function queuePreview(item) {
    const image = (item.references || []).find(ref => ref.type === "image" && ref.preview_url);
    return image ? `<img src="${escape(image.preview_url)}" alt="">` : `<span>${item.status === "running" ? "▶" : "↳"}</span>`;
  }

  function renderComposerQueue(pending) {
    const tray = byId("composer-queue-tray");
    if (!tray) return;
    const queued = pending.filter(item => item.status === "queued");
    tray.classList.toggle("hidden", !queued.length);
    tray.innerHTML = queued.map(item => `
      <div class="composer-queue-row" data-queue-id="${escape(item.id)}">
        <div class="composer-queue-preview">${queuePreview(item)}</div>
        <div class="composer-queue-copy"><strong>${escape(item.message)}</strong><small>${(item.references || []).length ? `${item.references.length} anexo(s) ou referência(s)` : "Aguardando a execução atual"}</small></div>
        <div class="composer-queue-actions">
          ${state.activeTurnId ? `<button type="button" class="composer-queue-steer" data-queue-steer="${escape(item.id)}" aria-label="Enviar esta direção ao turno em execução" title="Orientar o turno agora">Orientar agora</button>` : ""}
          <button type="button" class="composer-queue-edit" data-queue-edit="${escape(item.id)}" aria-label="Editar item da fila" title="Editar">Editar</button>
          <button type="button" class="composer-queue-delete" data-queue-delete="${escape(item.id)}" aria-label="Excluir da fila">⌫</button>
        </div>
      </div>`).join("");
  }

  async function addMessageToQueue() {
    const message = selectors.prompt.value.trim();
    if (!message) return;
    if (!state.activeThreadId) return sendMessage();
    try {
      await api(`/api/threads/${encodeURIComponent(state.activeThreadId)}/queue`, {method:"POST", body:JSON.stringify(activePayload(message))});
      selectors.prompt.value = "";
      state.references = [];
      state.composerMode = null;
      renderOperationReferences();
      renderComposerMode();
      autoResizePrompt();
      await loadOperationQueue();
      toast("Comando adicionado à fila.", "success");
    } catch (error) { toast(error.message, "error"); }
  }

  async function mutateQueue(action, itemId) {
    const index = state.queue.findIndex(item => item.id === itemId);
    if (index < 0) return;
    if (action === "steer") {
      const result = await api(`/api/threads/${encodeURIComponent(state.activeThreadId)}/queue/${encodeURIComponent(itemId)}/steer`, {method:"POST"});
      await loadOperationQueue();
      const message = result.steered
        ? "Direção enviada ao turno em execução."
        : result.started ? "O turno anterior terminou; a mensagem começou como próxima execução." : "A mensagem permanece na fila.";
      toast(message, result.steered || result.started ? "success" : "error");
      return;
    } else if (action === "delete") {
      await api(`/api/threads/${encodeURIComponent(state.activeThreadId)}/queue/${encodeURIComponent(itemId)}`, {method:"DELETE"});
    } else if (action === "edit") {
      const message = window.prompt("Editar comando da fila:", state.queue[index].message);
      if (message?.trim()) await api(`/api/threads/${encodeURIComponent(state.activeThreadId)}/queue/${encodeURIComponent(itemId)}`, {method:"PATCH", body:JSON.stringify({message:message.trim()})});
    } else {
      const offset = action === "up" ? -1 : 1;
      const next = index + offset;
      if (next < 0 || next >= state.queue.length) return;
      const ids = state.queue.map(item => item.id);
      [ids[index], ids[next]] = [ids[next], ids[index]];
      await api(`/api/threads/${encodeURIComponent(state.activeThreadId)}/queue/reorder`, {method:"POST", body:JSON.stringify({item_ids:ids})});
    }
    await loadOperationQueue();
  }

  function referenceCatalog() {
    const catalog = [
      {type:"desktop", id:"desktop-codex", name:"Desktop Codex", path:"remote:codex"},
      {type:"desktop", id:"desktop-ubuntu", name:"Desktop Ubuntu", path:"remote:desktop"},
      {type:"server", id:"sasocq-mini", name:"Servidor Mini PC", path:"sasocq-mini"},
      {type:"browser", id:"remote-browser", name:"Navegador no servidor", path:"remote:browser"},
      {type:"chrome", id:"local-chrome", name:"Chrome deste dispositivo", path:"chrome-bridge:optional"},
    ];
    for (const project of state.projects || []) {
      catalog.push({type:"project", id:project.id, name:project.name, path:project.path});
      for (const path of project.clc?.paths || []) catalog.push({type:"folder", id:`${project.id}:${path}`, name:path.split(/[\\/]/).pop() || path, path});
    }
    for (const thread of state.threads || []) catalog.push({type:"conversation", id:thread.id, name:thread.name || thread.preview || "Conversa", path:thread.id});
    for (const thread of state.chatgptReferences || []) catalog.push({...thread, type:"chatgpt"});
    for (const skill of state.extensions?.skills || []) catalog.push({type:"skill", id:skill.id || skill.name, name:skill.name, path:skill.path || ""});
    for (const app of state.extensions?.apps || []) catalog.push({type:"app", id:app.id || app.name, name:app.name, path:app.url || ""});
    for (const item of state.extensions?.mcp || []) catalog.push({type:"mcp", id:item.id || item.name, name:item.name, path:item.command || item.url || ""});
    return catalog.filter(item => item.id && item.name);
  }

  function renderMentionPicker(query = "") {
    const picker = byId("mention-picker");
    const normalized = query.trim().toLowerCase();
    const items = referenceCatalog().filter(item => {
      if (!normalized) return true;
      const searchable = item.type === "chatgpt" ? item.name : `${item.type} ${item.name} ${item.path || ""}`;
      return searchable.toLowerCase().includes(normalized);
    }).slice(0, 40);
    const results = items.map(item => `<button type="button" class="mention-option" data-reference-id="${escape(item.id)}" data-reference-type="${escape(item.type)}"><span>@${escape(item.type)}</span><strong>${escape(item.name)}</strong><small>${escape(item.type === "chatgpt" ? "Conversa da sua conta ChatGPT" : item.path || "")}</small></button>`).join("");
    const loading = state.chatgptReferenceLoading ? '<div class="mention-source-status">Buscando conversas do ChatGPT…</div>' : "";
    const empty = !items.length && !state.chatgptReferenceLoading
      ? '<div class="panel-empty">Nenhuma referência encontrada no Codex ou no ChatGPT.</div>'
      : "";
    picker.innerHTML = results + loading + empty;
    picker.classList.remove("hidden");
  }

  let chatgptSearchTimer = null;
  let chatgptSearchSequence = 0;
  async function refreshChatGPTReferences(query = "") {
    const sequence = ++chatgptSearchSequence;
    state.chatgptReferenceLoading = true;
    state.chatgptReferenceQuery = query;
    renderMentionPicker(query);
    try {
      const data = await api(`/api/references/chatgpt?query=${encodeURIComponent(query.trim())}&limit=40`);
      if (sequence !== chatgptSearchSequence) return;
      state.chatgptReferences = data.items || [];
    } catch (error) {
      if (sequence !== chatgptSearchSequence) return;
      state.chatgptReferences = [];
      console.warn("Conversas do ChatGPT indisponíveis", error);
    } finally {
      if (sequence === chatgptSearchSequence) {
        state.chatgptReferenceLoading = false;
        if (!byId("mention-picker")?.classList.contains("hidden")) renderMentionPicker(query);
      }
    }
  }

  function openMentionPicker(query = "") {
    state.chatgptReferenceQuery = query;
    renderMentionPicker(query);
    clearTimeout(chatgptSearchTimer);
    chatgptSearchTimer = setTimeout(() => refreshChatGPTReferences(query), query.trim() ? 250 : 0);
  }

  function addReference(type, id) {
    const item = referenceCatalog().find(ref => ref.type === type && String(ref.id) === String(id));
    if (!item || state.references.some(ref => ref.type === type && String(ref.id) === String(id))) return;
    state.references.push(item);
    renderOperationReferences();
    byId("mention-picker").classList.add("hidden");
    selectors.prompt.value = selectors.prompt.value.replace(/(?:^|\s)@[^@\n]*$/, match => match.startsWith(" ") ? " " : "");
    autoResizePrompt();
    selectors.prompt.focus();
  }

  window.renderOperationReferences = function renderOperationReferences() {
    const root = byId("reference-chips");
    if (!root) return;
    root.innerHTML = (state.references || []).map((ref, index) => {
      if (["file", "image"].includes(ref.type)) {
        const preview = ref.type === "image" && ref.preview_url
          ? `<img src="${escape(ref.preview_url)}" alt="">`
          : '<span class="attachment-symbol">▧</span>';
        return `<span class="reference-chip attachment-chip">${preview}<span>${escape(ref.name)}</span><button type="button" data-remove-reference="${index}" aria-label="Remover anexo">×</button></span>`;
      }
      return `<span class="reference-chip"><b>@${escape(ref.type)}</b> ${escape(ref.name)} <button type="button" data-remove-reference="${index}" aria-label="Remover referência">×</button></span>`;
    }).join("");
  };

  window.renderComposerMode = function renderComposerMode() {
    const button = byId("composer-mode");
    if (!button) return;
    button.classList.toggle("hidden", !state.composerMode);
    button.textContent = state.composerMode === "goal" ? "Meta ativa" : state.composerMode === "plan" ? "Planejamento ativo" : "";
  };

  async function uploadAttachment(file) {
    if (!state.activeProject) throw new Error("Selecione um projeto antes de anexar arquivos.");
    const query = new URLSearchParams({filename:file.name, relative_path:file.webkitRelativePath || file.name});
    const data = await api(`/api/projects/${encodeURIComponent(state.activeProject.id)}/attachments?${query}`, {
      method:"POST",
      headers:{"Content-Type":file.type || "application/octet-stream"},
      body:file,
    });
    return data.attachment;
  }

  async function addFiles(files) {
    const selected = [...(files || [])].filter(file => file?.size >= 0).slice(0, 100);
    if (!selected.length) return;
    const added = [];
    for (let index = 0; index < selected.length; index += 3) {
      added.push(...await Promise.all(selected.slice(index, index + 3).map(uploadAttachment)));
    }
    for (const attachment of added) {
      if (!state.references.some(ref => ref.type === attachment.type && ref.id === attachment.id)) state.references.push(attachment);
    }
    renderOperationReferences();
    toast(`${added.length} arquivo(s) anexado(s) ao projeto.`, "success");
  }

  function toggleComposerMode(mode) {
    state.composerMode = state.composerMode === mode ? null : mode;
    renderComposerMode();
    byId("add-menu")?.classList.add("hidden");
    selectors.prompt.focus();
  }

  function decorateThreadRows() {
    [...selectors.threadList.querySelectorAll(".nav-item")].forEach((row, index) => {
      const thread = state.threads.filter(thread => {
        const q = byId("thread-search").value.trim().toLowerCase();
        return !q || `${thread.name || ""} ${thread.preview || ""}`.toLowerCase().includes(q);
      })[index];
      if (!thread) return;
      row.dataset.threadId = thread.id;
      if (thread.clc?.pinned && !row.querySelector(".pin-mark")) row.querySelector(".nav-copy")?.insertAdjacentHTML("afterbegin", '<span class="pin-mark" title="Fixada">◆</span>');
      row.insertAdjacentHTML("beforeend", `<span class="row-menu-button" role="button" tabindex="0" data-thread-menu="${escape(thread.id)}" aria-label="Opções da conversa">•••</span>`);
    });
  }

  function decorateProjectRows() {
    [...selectors.projectList.querySelectorAll(".project-nav-item")].forEach(row => {
      const project = state.projects.find(item => item.id === row.dataset.projectId);
      if (!project) return;
      if (row.querySelector(".project-icon-menu") || row.querySelector("[data-project-pin]")) return;
      row.insertAdjacentHTML("beforeend", `<span class="row-menu-button" role="button" tabindex="0" data-project-pin="${escape(project.id)}" title="${project.clc?.pinned ? "Desafixar" : "Fixar"}">${project.clc?.pinned ? "◆" : "◇"}</span>`);
    });
  }

  const baseRenderThreads = renderThreads;
  renderThreads = function operationsRenderThreads() { baseRenderThreads(); decorateThreadRows(); };
  const baseRenderProjects = renderProjects;
  renderProjects = function operationsRenderProjects() { baseRenderProjects(); decorateProjectRows(); };
  loadThreads = async function operationsLoadThreads() {
    const generation = ++state.threadLoadGeneration;
    const projects = state.activeProject ? [state.activeProject] : [...state.projects];
    const useCache = !state.archivedThreads;
    const results = projects.map(project => useCache ? (state.projectThreads.get(project.id) || []) : []);
    const publish = () => {
      if (generation !== state.threadLoadGeneration) return false;
      state.threads = results.flat().sort((left, right) => threadActivityTimestamp(right) - threadActivityTimestamp(left));
      renderProjects();
      renderThreads();
      renderOtherConversationApprovals();
      return true;
    };
    publish();

    // Project workers are initialized lazily by the API. A bridge that was not
    // running at page load still needs to be queried so its conversations show.
    let nextIndex = 0;
    const worker = async () => {
      while (generation === state.threadLoadGeneration) {
        const index = nextIndex++;
        if (index >= projects.length) return;
        const project = projects[index];
        try {
          const requestLimit = state.activeProject ? PROJECT_CONVERSATION_LIMIT : 24;
          const data = await api(`/api/threads?project_id=${encodeURIComponent(project.id)}&archived=${state.archivedThreads}&limit=${requestLimit}`);
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
          if (useCache) state.projectThreads.set(project.id, threads);
          results[index] = threads;
          state.projectActivity.set(project.id, threads.reduce((latest, thread) => Math.max(latest, threadActivityTimestamp(thread)), 0));
          publish();
        } catch (error) {
          addActivity(`Conversas • ${project.name}`, error.message, "error");
        }
      }
    };
    await Promise.all(Array.from({length:Math.min(3, projects.length)}, worker));
    if (useCache) persistThreadSummaryCache();
    publish();
    return state.threads;
  };
  const baseOpenThread = openThread;
  openThread = async function operationsOpenThread(id) { await baseOpenThread(id); await loadOperationQueue(); };
  const baseClearProjectSelection = clearProjectSelection;
  clearProjectSelection = async function operationsClearProjectSelection() {
    state.queue = [];
    renderOperationQueue();
    await baseClearProjectSelection();
  };
  const baseNewThread = newThread;
  newThread = async function operationsNewThread() { await baseNewThread(); state.queue = []; state.references = []; state.composerMode = null; renderOperationQueue(); renderOperationReferences(); renderComposerMode(); };
  const baseSelectProject = selectProject;
  selectProject = async function operationsSelectProject(id) { await baseSelectProject(id); state.queue = []; state.references = []; state.composerMode = null; renderOperationQueue(); renderOperationReferences(); renderComposerMode(); };
  const baseUpdateRunningUI = updateRunningUI;
  updateRunningUI = function operationsRunningUI() {
    baseUpdateRunningUI();
    const running = Boolean(state.activeTurnId);
    byId("steer-button")?.classList.add("hidden");
    byId("queue-button").classList.toggle("hidden", !running);
  };
  const baseHandleEvent = handleEvent;
  handleEvent = function operationsHandleEvent(event) {
    baseHandleEvent(event);
    if (event.kind === "notification" && ["turn/started", "turn/completed"].includes(event.notification?.method)) loadOperationQueue();
  };

  function openThreadMenu(threadId, anchor) {
    document.querySelector(".floating-row-menu")?.remove();
    const thread = state.threads.find(item => item.id === threadId) || {};
    const menu = document.createElement("div");
    menu.className = "floating-row-menu";
    menu.innerHTML = `
      <button data-thread-action="${thread.clc?.pinned ? "unpin" : "pin"}" data-thread-id="${escape(threadId)}">${thread.clc?.pinned ? "Desafixar" : "Fixar"}</button>
      <button data-thread-action="rename" data-thread-id="${escape(threadId)}">Renomear</button>
      <button data-thread-action="duplicate" data-thread-id="${escape(threadId)}">Duplicar</button>
      <button data-thread-action="${state.archivedThreads ? "restore" : "archive"}" data-thread-id="${escape(threadId)}">${state.archivedThreads ? "Restaurar" : "Arquivar"}</button>
      <button class="danger" data-thread-action="delete" data-thread-id="${escape(threadId)}">Excluir</button>`;
    const rect = anchor.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(window.innerWidth - 210, rect.right - 190))}px`;
    menu.style.top = `${Math.min(window.innerHeight - 230, rect.bottom + 4)}px`;
    document.body.appendChild(menu);
  }

  async function threadAction(action, threadId) {
    document.querySelector(".floating-row-menu")?.remove();
    const thread = state.threads.find(item => item.id === threadId) || {};
    if (["pin", "unpin"].includes(action)) await api(`/api/threads/${encodeURIComponent(threadId)}/metadata`, {method:"PATCH", body:JSON.stringify({pinned:action === "pin"})});
    if (action === "rename") {
      const name = window.prompt("Nome da conversa:", thread.name || thread.preview || "");
      if (name?.trim()) await api(`/api/threads/${encodeURIComponent(threadId)}/name`, {method:"PATCH", body:JSON.stringify({name:name.trim()})});
    }
    if (action === "duplicate") await api(`/api/threads/${encodeURIComponent(threadId)}/duplicate`, {method:"POST"});
    if (action === "archive") await api(`/api/threads/${encodeURIComponent(threadId)}/archive`, {method:"POST"});
    if (action === "restore") await api(`/api/threads/${encodeURIComponent(threadId)}/restore`, {method:"POST"});
    if (action === "delete" && window.confirm("Excluir definitivamente esta conversa?")) await api(`/api/threads/${encodeURIComponent(threadId)}`, {method:"DELETE"});
    await loadThreads();
    if (["archive", "delete"].includes(action) && state.activeThreadId === threadId) await newThread();
  }

  function activateOperationView(view) {
    document.querySelectorAll(".rail-button").forEach(button => button.classList.toggle("active", button.dataset.operationView === view));
    closeMobilePanels();
    if (view === "chat") return;
    if (view === "projects") return openProjectManager("manage").catch(error => toast(error.message, "error"));
    if (view === "queue") { setInspectorTab("queue"); return loadOperationQueue(); }
    if (view === "plugins") return openToolsDialog().then(() => { state.toolsTab = "plugins"; renderToolsDialog(); });
    if (view === "remote") { setInspectorTab("screen"); return refreshRemoteStatus(); }
    if (view === "codex") return openTerminal();
    if (view === "system") byId("open-system").click();
  }

  document.addEventListener("click", event => {
    const rail = event.target.closest("[data-operation-view]");
    if (rail) activateOperationView(rail.dataset.operationView);
    const menu = event.target.closest("[data-thread-menu]");
    if (menu) { event.preventDefault(); event.stopPropagation(); openThreadMenu(menu.dataset.threadMenu, menu); }
    const action = event.target.closest("[data-thread-action]");
    if (action) threadAction(action.dataset.threadAction, action.dataset.threadId).catch(error => toast(error.message, "error"));
    const pin = event.target.closest("[data-project-pin]");
    if (pin) {
      event.preventDefault(); event.stopPropagation();
      const project = state.projects.find(item => item.id === pin.dataset.projectPin);
      api(`/api/projects/${encodeURIComponent(pin.dataset.projectPin)}/metadata`, {method:"PATCH", body:JSON.stringify({pinned:!project?.clc?.pinned})}).then(loadProjects).catch(error => toast(error.message, "error"));
    }
    const reference = event.target.closest("[data-reference-id]");
    if (reference) addReference(reference.dataset.referenceType, reference.dataset.referenceId);
    const remove = event.target.closest("[data-remove-reference]");
    if (remove) { state.references.splice(Number(remove.dataset.removeReference), 1); renderOperationReferences(); }
    for (const operation of ["steer", "up", "down", "edit", "delete"]) {
      const target = event.target.closest(`[data-queue-${operation}]`);
      if (target) mutateQueue(operation, target.dataset[`queue${operation[0].toUpperCase()}${operation.slice(1)}`]).catch(error => toast(error.message, "error"));
    }
    if (!event.target.closest(".floating-row-menu") && !menu) document.querySelector(".floating-row-menu")?.remove();
    if (!event.target.closest("#mention-picker") && !event.target.closest("#mention-button")) byId("mention-picker")?.classList.add("hidden");
  }, true);

  byId("mention-button")?.addEventListener("click", () => openMentionPicker());
  byId("queue-button")?.addEventListener("click", addMessageToQueue);
  byId("composer-mode")?.addEventListener("click", () => toggleComposerMode(state.composerMode));
  byId("add-button")?.addEventListener("click", event => {
    event.stopPropagation();
    byId("add-menu")?.classList.toggle("hidden");
  });
  byId("add-menu")?.addEventListener("click", event => {
    const action = event.target.closest("[data-add-action]")?.dataset.addAction;
    if (!action) return;
    if (action === "files") byId("composer-file-input")?.click();
    if (action === "images") byId("composer-image-input")?.click();
    if (action === "folder") byId("composer-folder-input")?.click();
    if (action === "goal") toggleComposerMode("goal");
    if (action === "plan") toggleComposerMode("plan");
    if (action === "plugins") {
      byId("add-menu")?.classList.add("hidden");
      openToolsDialog().then(() => { state.toolsTab = "plugins"; renderToolsDialog(); }).catch(error => toast(error.message, "error"));
    }
  });
  for (const id of ["composer-file-input", "composer-image-input", "composer-folder-input"]) {
    byId(id)?.addEventListener("change", event => {
      addFiles(event.target.files).catch(error => toast(error.message, "error"));
      event.target.value = "";
      byId("add-menu")?.classList.add("hidden");
    });
  }
  const composerWrap = document.querySelector(".composer-wrap");
  composerWrap?.addEventListener("dragover", event => { event.preventDefault(); composerWrap.classList.add("drop-target"); });
  composerWrap?.addEventListener("dragleave", event => { if (!composerWrap.contains(event.relatedTarget)) composerWrap.classList.remove("drop-target"); });
  composerWrap?.addEventListener("drop", event => {
    event.preventDefault(); composerWrap.classList.remove("drop-target");
    addFiles(event.dataTransfer?.files).catch(error => toast(error.message, "error"));
  });
  selectors.prompt?.addEventListener("paste", event => {
    const files = [...(event.clipboardData?.items || [])].filter(item => item.kind === "file").map(item => item.getAsFile()).filter(Boolean);
    if (files.length) addFiles(files).catch(error => toast(error.message, "error"));
  });
  document.addEventListener("click", event => {
    if (!event.target.closest("#add-menu, #add-button")) byId("add-menu")?.classList.add("hidden");
  });
  byId("context-open-terminal")?.addEventListener("click", openTerminal);
  byId("context-open-tools")?.addEventListener("click", openToolsDialog);
  byId("mobile-nav-projects")?.addEventListener("click", () => activateOperationView("projects"));
  byId("mobile-nav-queue")?.addEventListener("click", () => activateOperationView("queue"));
  const archivedToggle = document.createElement("button");
  archivedToggle.id = "toggle-archived-threads";
  archivedToggle.className = "ghost-button archived-toggle";
  archivedToggle.type = "button";
  archivedToggle.textContent = "Arquivadas";
  byId("thread-search")?.insertAdjacentElement("afterend", archivedToggle);
  archivedToggle.addEventListener("click", async () => {
    state.archivedThreads = !state.archivedThreads;
    archivedToggle.classList.toggle("active", state.archivedThreads);
    archivedToggle.textContent = state.archivedThreads ? "← Ativas" : "Arquivadas";
    await loadThreads();
  });
  selectors.prompt.addEventListener("input", () => {
    const match = selectors.prompt.value.match(/(?:^|\s)@([^@\n]*)$/);
    if (match) openMentionPicker(match[1]);
  });
  selectors.prompt.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && state.activeTurnId && !event.isComposing) {
      event.preventDefault();
      addMessageToQueue();
    }
  }, true);

  // The system shortcut opens an authenticated Codex session immediately.
  byId("open-terminal")?.addEventListener("click", event => {
    if (event.altKey) return;
    event.preventDefault(); event.stopImmediatePropagation(); openTerminal();
  }, true);
  byId("open-tools")?.addEventListener("click", event => {
    if (event.altKey) return;
    event.preventDefault(); event.stopImmediatePropagation(); setInspectorTab("tools");
  }, true);

  loadOperationMetadata().then(() => { renderProjects(); renderThreads(); });
  renderOperationQueue();
  renderOperationReferences();
})();
