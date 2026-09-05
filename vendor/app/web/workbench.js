/* Local SASOCQ production workbench: artifacts, Git, agents, memory and guided replay. */
(() => {
  "use strict";

  const dialog = document.getElementById("workbench-dialog");
  const content = document.getElementById("workbench-content");
  const subtitle = document.getElementById("workbench-subtitle");
  const escape = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
  const api = (...args) => window.dexApi(...args);
  const toast = (message, kind = "") => window.dexToast?.(message, kind);
  const current = () => window.dexWorkbenchContext?.() || {};
  const projectId = () => current().project?.id || "";
  const endpoint = suffix => `/api/workbench/${encodeURIComponent(projectId())}${suffix}`;
  const formatBytes = value => {
    const bytes = Number(value || 0);
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
  };
  const formatDate = value => { try { return new Intl.DateTimeFormat("pt-BR", {dateStyle:"short", timeStyle:"short"}).format(new Date(Number(value) * 1000)); } catch { return ""; } };

  const wb = {
    tab: "overview",
    summary: null,
    artifacts: [],
    artifactsMeta: {count:0, truncated:false},
    annotations: [],
    artifactQuery: "",
    compare: new Set(),
    git: {repo:".", status:null, diff:"", scope:"working", comments:[]},
    worktrees: [],
    memory: {enabled:false, items:[]},
    playbooks: [],
    recording: false,
    recordedSteps: [],
    recognition: null,
  };

  async function loadSummary() {
    wb.summary = await api(endpoint("/summary"));
    subtitle.textContent = `${wb.summary.project.name} • ${wb.summary.project.path}`;
  }

  function overviewHTML() {
    const value = wb.summary;
    if (!value) return '<div class="panel-empty">Carregando recursos…</div>';
    return `<div class="workbench-grid">
      <article class="workbench-card"><h3>Artefatos</h3><strong class="metric">DOC · PDF</strong><p>Prévia segura de documentos, planilhas, apresentações, imagens, HTML e texto.</p><button data-workbench-open="artifacts">Abrir artefatos</button></article>
      <article class="workbench-card"><h3>Git e revisão</h3><strong class="metric">${value.repositories.length}</strong><p>${value.repositories.length ? `${value.repositories.reduce((sum, repo) => sum + repo.changes, 0)} alteração(ões) em repositórios detectados.` : "Nenhum repositório Git neste projeto."}</p><button data-workbench-open="git" ${value.repositories.length ? "" : "disabled"}>Revisar mudanças</button></article>
      <article class="workbench-card"><h3>Worktrees</h3><strong class="metric">Paralelo</strong><p>Crie branches isoladas para conversas simultâneas sem misturar alterações.</p><button data-workbench-open="worktrees" ${value.repositories.length ? "" : "disabled"}>Gerenciar worktrees</button></article>
      <article class="workbench-card"><h3>Agentes</h3><strong class="metric">Ao vivo</strong><p>Veja o agente principal e subagentes registrados na conversa atual.</p><button data-workbench-open="agents">Ver agentes</button></article>
      <article class="workbench-card"><h3>Memória local</h3><strong class="metric">${value.memory.count}</strong><p>${value.memory.enabled ? "Ativa por opção do operador; somente itens salvos manualmente." : "Desativada por padrão e sem captura passiva de histórico."}</p><button data-workbench-open="memory">Configurar memória</button></article>
      <article class="workbench-card"><h3>Voz e contexto visual</h3><strong class="metric">🎙 ▧</strong><p>Dite no compositor, leia a última resposta ou anexe uma captura atual da tela Linux.</p><div class="workbench-actions"><button data-workbench-read-last>Ler resposta</button><button data-workbench-appshot>Capturar tela</button></div></article>
    </div>`;
  }

  async function loadArtifacts() {
    const query = new URLSearchParams({query:wb.artifactQuery, limit:"120"});
    const [artifacts, annotations] = await Promise.all([api(endpoint(`/artifacts?${query}`)), api(endpoint("/artifacts/annotations"))]);
    wb.artifacts = artifacts.items || [];
    wb.artifactsMeta = {count:Number(artifacts.count || 0), truncated:Boolean(artifacts.truncated)};
    wb.annotations = annotations.annotations || [];
  }

  function artifactsHTML() {
    const rows = wb.artifacts.map(item => `<div class="workbench-row">
      <div class="workbench-row-copy"><strong>${escape(item.name)}</strong><small>${escape(item.path)} • ${formatBytes(item.size)} • ${formatDate(item.modified_at)}</small></div>
      <div class="workbench-actions"><label title="Selecionar para comparar"><input type="checkbox" data-artifact-compare="${escape(item.path)}" ${wb.compare.has(item.path) ? "checked" : ""}> comparar</label><button data-artifact-preview="${escape(item.path)}" data-artifact-kind="${escape(item.kind)}">Visualizar</button><button data-artifact-annotate="${escape(item.path)}">Anotar</button><a href="${endpoint(`/artifacts/download?path=${encodeURIComponent(item.path)}`)}" download><button>Baixar</button></a></div>
    </div>`).join("");
    const annotations = wb.annotations.map(item => `<div class="workbench-row"><div class="workbench-row-copy"><strong>${escape(item.path)}${item.page ? ` • página ${item.page}` : ""}</strong><small>${escape(item.body)}</small></div><button class="danger" data-artifact-delete-annotation="${escape(item.id)}">Excluir</button></div>`).join("");
    const resultNote = wb.artifactsMeta.truncated
      ? `<div class="workbench-notice">Mostrando os ${wb.artifactsMeta.count} primeiros resultados. Refine a busca por nome ou pasta para localizar outros arquivos.</div>`
      : `<div class="workbench-notice">${wb.artifactsMeta.count} artefato(s) encontrado(s).</div>`;
    return `<div class="workbench-toolbar"><input id="artifact-search" type="search" placeholder="Buscar nome ou pasta" value="${escape(wb.artifactQuery)}"><button data-artifact-refresh>Buscar</button><button data-artifact-compare-selected ${wb.compare.size === 2 ? "" : "disabled"}>Comparar 2 versões</button></div>${resultNote}<div class="workbench-list">${rows || '<div class="panel-empty">Nenhum artefato compatível encontrado.</div>'}</div><section style="margin-top:18px"><h3>Anotações</h3><div class="workbench-list">${annotations || '<div class="panel-empty">Nenhuma anotação nos artefatos.</div>'}</div></section>`;
  }

  async function previewArtifact(path, kind) {
    if (kind === "office") await api(endpoint(`/artifacts/render?path=${encodeURIComponent(path)}`), {method:"POST"});
    content.innerHTML = `<div class="workbench-toolbar"><button data-workbench-open="artifacts">← Voltar</button><strong>${escape(path)}</strong><a href="${endpoint(`/artifacts/download?path=${encodeURIComponent(path)}`)}" download><button>Baixar</button></a></div><iframe class="workbench-preview" sandbox title="Prévia segura do artefato" src="${endpoint(`/artifacts/preview?path=${encodeURIComponent(path)}`)}"></iframe>`;
  }

  async function compareArtifacts() {
    const [left, right] = [...wb.compare];
    const values = await Promise.all([left, right].map(path => fetch(endpoint(`/artifacts/preview?path=${encodeURIComponent(path)}`), {credentials:"same-origin"}).then(response => response.text())));
    content.innerHTML = `<div class="workbench-toolbar"><button data-workbench-open="artifacts">← Voltar</button><strong>Comparação lado a lado</strong></div><div class="workbench-grid" style="grid-template-columns:1fr 1fr"><article class="workbench-card"><h3>${escape(left)}</h3><pre class="workbench-diff">${escape(values[0])}</pre></article><article class="workbench-card"><h3>${escape(right)}</h3><pre class="workbench-diff">${escape(values[1])}</pre></article></div>`;
  }

  async function loadGit() {
    if (!wb.summary?.repositories.length) return;
    if (!wb.summary.repositories.some(repo => repo.path === wb.git.repo)) wb.git.repo = wb.summary.repositories[0].path;
    const query = new URLSearchParams({repo:wb.git.repo});
    const [status, comments] = await Promise.all([api(endpoint(`/git/status?${query}`)), api(endpoint("/git/comments"))]);
    wb.git.status = status;
    wb.git.comments = comments.comments || [];
  }

  function gitHTML() {
    if (!wb.summary?.repositories.length) return '<div class="panel-empty">Nenhum repositório Git detectado neste projeto.</div>';
    const repositories = wb.summary.repositories.map(repo => `<option value="${escape(repo.path)}" ${repo.path === wb.git.repo ? "selected" : ""}>${escape(repo.name)} • ${escape(repo.branch)}</option>`).join("");
    const changes = (wb.git.status?.changes || []).map(change => `<div class="workbench-row"><div class="workbench-row-copy"><strong>${escape(change.path)}</strong><small>índice ${escape(change.index)} • trabalho ${escape(change.worktree)}</small></div><label><input type="checkbox" data-git-path="${escape(change.path)}"> selecionar</label></div>`).join("");
    const comments = wb.git.comments.map(comment => `<div class="workbench-row"><div class="workbench-row-copy"><strong>${escape(comment.path)}:${comment.line}</strong><small>${escape(comment.body)}</small></div><button class="danger" data-git-delete-comment="${escape(comment.id)}">Excluir</button></div>`).join("");
    return `<div class="workbench-toolbar"><select id="git-repo">${repositories}</select><button data-git-refresh>Atualizar</button><button data-git-diff="working">Diff local</button><button data-git-diff="staged">Diff preparado</button><button data-git-diff="head">Último commit</button></div>
      <div class="workbench-card"><h3>${escape(wb.git.status?.branch || "Git")}</h3><div class="workbench-actions"><button data-git-action="stage">Preparar</button><button data-git-action="unstage">Retirar da preparação</button><button class="danger" data-git-action="revert">Descartar selecionados</button></div><div class="workbench-list">${changes || '<div class="panel-empty">Árvore de trabalho limpa.</div>'}</div></div>
      <div class="workbench-grid" style="margin-top:14px;grid-template-columns:2fr 1fr"><article class="workbench-card"><h3>Commit e envio</h3><div class="workbench-form"><input id="git-commit-message" placeholder="Mensagem do commit"><div class="workbench-actions"><button class="primary" data-git-action="commit">Criar commit</button><button data-git-action="push">Enviar ao remoto</button></div></div></article><article class="workbench-card"><h3>Comentário de revisão</h3><div class="workbench-form"><input id="git-comment-path" placeholder="arquivo"><input id="git-comment-line" type="number" min="1" placeholder="linha"><textarea id="git-comment-body" placeholder="Comentário"></textarea><button data-git-add-comment>Adicionar comentário</button></div></article></div>
      <section style="margin-top:14px"><h3>Comentários</h3><div class="workbench-list">${comments || '<div class="panel-empty">Nenhum comentário de revisão.</div>'}</div></section>`;
  }

  async function showDiff(scope) {
    wb.git.scope = scope;
    const query = new URLSearchParams({repo:wb.git.repo, scope});
    const data = await api(endpoint(`/git/diff?${query}`));
    wb.git.diff = data.diff || "";
    content.innerHTML = `<div class="workbench-toolbar"><button data-workbench-open="git">← Voltar</button><strong>${escape(scope)}</strong></div><pre class="workbench-diff">${escape(wb.git.diff || "Nenhuma diferença neste escopo.")}</pre>`;
  }

  function selectedGitPaths() { return [...content.querySelectorAll("[data-git-path]:checked")].map(input => input.dataset.gitPath); }

  async function gitAction(action) {
    const payload = {action, repo:wb.git.repo, paths:selectedGitPaths(), message:"", confirm:false};
    if (action === "commit") payload.message = document.getElementById("git-commit-message")?.value.trim() || "";
    if (action === "revert") payload.confirm = window.confirm("Descartar definitivamente as alterações locais dos arquivos selecionados?");
    if (action === "push") payload.confirm = window.confirm("Enviar os commits atuais ao repositório remoto?");
    if (["revert", "push"].includes(action) && !payload.confirm) return;
    const result = await api(endpoint("/git/action"), {method:"POST", body:JSON.stringify(payload)});
    toast(result.output?.trim() || "Operação Git concluída.", "success");
    await loadGit(); render();
  }

  async function loadWorktrees() {
    if (!wb.summary?.repositories.length) return;
    const query = new URLSearchParams({repo:wb.git.repo});
    wb.worktrees = (await api(endpoint(`/worktrees?${query}`))).worktrees || [];
  }

  function worktreesHTML() {
    if (!wb.summary?.repositories.length) return '<div class="panel-empty">Adicione um repositório Git antes de criar worktrees.</div>';
    const rows = wb.worktrees.map(item => `<div class="workbench-row"><div class="workbench-row-copy"><strong>${escape(item.branch || item.HEAD || "worktree")}</strong><small>${escape(item.worktree || "")}${item.bare ? " • bare" : ""}</small></div>${item.worktree && item.worktree !== wb.summary.project.path ? `<button class="danger" data-worktree-remove="${escape(item.worktree)}">Remover</button>` : "<span>Principal</span>"}</div>`).join("");
    return `<div class="workbench-grid" style="grid-template-columns:1fr 2fr"><article class="workbench-card"><h3>Novo worktree</h3><p>Cria uma branch e pasta isoladas ao lado do projeto principal.</p><div class="workbench-form"><input id="worktree-name" placeholder="nome-curto"><input id="worktree-branch" placeholder="branch (opcional)"><input id="worktree-base" value="HEAD" placeholder="base"><button class="primary" data-worktree-create>Criar worktree</button></div></article><section><div class="workbench-list">${rows || '<div class="panel-empty">Nenhum worktree listado.</div>'}</div></section></div>`;
  }

  function agentCandidates() {
    const root = current();
    const candidates = [];
    const seen = new Set();
    function visit(value, depth = 0) {
      if (!value || depth > 8) return;
      if (Array.isArray(value)) return value.slice(0, 500).forEach(item => visit(item, depth + 1));
      if (typeof value !== "object") return;
      const type = String(value.type || value.kind || value.name || "");
      if (/subagent|collab.*agent|agent.*tool/i.test(type)) {
        const id = String(value.id || value.agentId || value.callId || crypto.randomUUID());
        if (!seen.has(id)) { seen.add(id); candidates.push({id, type, status:value.status || value.state || "registrado", task:value.task || value.prompt || value.message || "Atividade delegada"}); }
      }
      Object.values(value).slice(0, 200).forEach(item => visit(item, depth + 1));
    }
    visit(root.thread); visit(root.items);
    return candidates;
  }

  function agentsHTML() {
    const context = current();
    const agents = agentCandidates();
    return `<div class="agent-tree"><article class="agent-node"><strong>Agente principal</strong><small>${context.running ? "Em execução" : "Aguardando"} • conversa ${escape(context.threadId || "ainda não iniciada")}</small></article>${agents.map(agent => `<article class="agent-node"><strong>${escape(agent.type || "Subagente")}</strong><small>${escape(agent.status)} • ${escape(String(agent.task).slice(0, 500))}</small></article>`).join("") || '<div class="panel-empty">Nenhum subagente foi registrado nesta conversa. Quando o app-server emitir itens de colaboração, eles aparecerão aqui.</div>'}</div>`;
  }

  async function loadMemory() { wb.memory = await api(endpoint("/memory")); }
  function memoryHTML() {
    const rows = (wb.memory.items || []).map(item => `<div class="workbench-row"><div class="workbench-row-copy"><strong>${escape((item.tags || []).join(" · ") || "Memória")}</strong><small>${escape(item.text)}</small></div><button class="danger" data-memory-delete="${escape(item.id)}">Excluir</button></div>`).join("");
    return `<article class="workbench-card"><label class="workbench-switch"><input id="memory-enabled" type="checkbox" ${wb.memory.enabled ? "checked" : ""}><strong>Usar memória local neste projeto</strong></label><p>Desativada por padrão. O Dex só usa itens salvos manualmente e nunca captura passivamente a tela ou o histórico do computador.</p></article><div class="workbench-grid" style="grid-template-columns:1fr 2fr;margin-top:14px"><article class="workbench-card"><h3>Guardar informação</h3><div class="workbench-form"><textarea id="memory-text" placeholder="Preferência, decisão ou contexto durável"></textarea><input id="memory-tags" placeholder="tags separadas por vírgula"><button class="primary" data-memory-add>Salvar memória</button></div></article><section><div class="workbench-list">${rows || '<div class="panel-empty">Nenhuma memória salva.</div>'}</div></section></div>`;
  }

  async function loadPlaybooks() { wb.playbooks = (await api(endpoint("/playbooks"))).playbooks || []; }
  function playbooksHTML() {
    const rows = wb.playbooks.map(item => `<div class="workbench-row"><div class="workbench-row-copy"><strong>${escape(item.name)}</strong><small>${item.steps.length} passo(s) • ${escape(item.notes || "sem observações")}</small></div><div class="workbench-actions"><button data-playbook-replay="${escape(item.id)}">Preparar execução</button><button class="danger" data-playbook-delete="${escape(item.id)}">Excluir</button></div></div>`).join("");
    return `<article class="workbench-card"><h3>Demonstrar e transformar em rotina</h3><p>O gravador registra somente os nomes das ações feitas na interface do Dex, nunca senhas nem valores digitados. A reprodução vira uma solicitação revisável no compositor e continua sujeita às aprovações normais.</p><div class="workbench-actions"><button class="${wb.recording ? "recording" : "primary"}" data-playbook-record>${wb.recording ? "■ Parar e salvar" : "● Iniciar demonstração"}</button><span>${wb.recordedSteps.length} passo(s) capturados</span></div></article><div class="workbench-list" style="margin-top:14px">${rows || '<div class="panel-empty">Nenhuma rotina demonstrada.</div>'}</div>`;
  }

  async function loadTab() {
    if (!projectId()) return;
    if (!wb.summary) await loadSummary();
    if (wb.tab === "artifacts") await loadArtifacts();
    if (wb.tab === "git") await loadGit();
    if (wb.tab === "worktrees") { await loadGit(); await loadWorktrees(); }
    if (wb.tab === "memory") await loadMemory();
    if (wb.tab === "playbooks") await loadPlaybooks();
  }

  function render() {
    document.querySelectorAll("[data-workbench-tab]").forEach(button => button.classList.toggle("active", button.dataset.workbenchTab === wb.tab));
    const html = ({overview:overviewHTML, artifacts:artifactsHTML, git:gitHTML, worktrees:worktreesHTML, agents:agentsHTML, memory:memoryHTML, playbooks:playbooksHTML})[wb.tab]?.();
    content.innerHTML = html || '<div class="panel-empty">Área indisponível.</div>';
  }

  async function selectTab(tab) {
    wb.tab = tab;
    content.innerHTML = '<div class="panel-empty">Carregando…</div>';
    await loadTab(); render();
  }

  window.openDexWorkbench = async () => {
    if (!projectId()) return toast("Selecione um projeto antes de abrir a central.", "error");
    wb.summary = null;
    if (!dialog.open) dialog.showModal();
    try { await selectTab(wb.tab); } catch (error) { content.innerHTML = `<div class="panel-empty">${escape(error.message)}</div>`; }
  };

  async function captureAppshot() {
    if (!projectId()) return toast("Selecione um projeto primeiro.", "error");
    const image = await fetch("/api/desktop/screenshot", {credentials:"same-origin"});
    if (!image.ok) throw new Error("Não foi possível capturar a tela Linux.");
    const blob = await image.blob();
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const query = new URLSearchParams({filename:`appshot-${stamp}.png`});
    const uploaded = await api(`/api/projects/${encodeURIComponent(projectId())}/attachments?${query}`, {method:"POST", headers:{"Content-Type":"image/png"}, body:blob});
    const attachment = uploaded.attachment;
    window.dexAddReference?.({type:"image", id:attachment.id, name:attachment.name, path:attachment.path});
    toast("Captura anexada à próxima mensagem.", "success");
  }

  function toggleVoice() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) return toast("Este navegador não oferece ditado por voz.", "error");
    if (wb.recognition) { wb.recognition.stop(); return; }
    const recognition = new SpeechRecognition();
    recognition.lang = "pt-BR";
    recognition.interimResults = true;
    recognition.continuous = true;
    const original = document.getElementById("prompt")?.value || "";
    recognition.onresult = event => {
      let text = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) text += event.results[index][0].transcript;
      window.dexSetPrompt?.(`${original}${original && text ? " " : ""}${text}`);
    };
    recognition.onend = () => { wb.recognition = null; document.getElementById("workbench-voice")?.classList.remove("recording"); };
    recognition.onerror = event => toast(`Ditado interrompido: ${event.error}`, "error");
    wb.recognition = recognition;
    document.getElementById("workbench-voice")?.classList.add("recording");
    recognition.start();
    toast("Ditado ativo. O processamento segue a política de voz do navegador.");
  }

  function readLastResponse() {
    const messages = [...document.querySelectorAll("#message-list .message, #message-list [data-role='assistant']")];
    const text = messages.reverse().map(node => node.innerText?.trim()).find(Boolean);
    if (!text || !window.speechSynthesis) return toast("Não há resposta disponível para leitura.", "error");
    speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text.slice(0, 12000));
    utterance.lang = "pt-BR";
    speechSynthesis.speak(utterance);
  }

  function recordAction(event) {
    if (!wb.recording) return;
    const target = event.target.closest("button, a, summary, [role='button']");
    if (!target || target.closest("#workbench-dialog")?.querySelector("[data-playbook-record]") === target) return;
    const label = target.getAttribute("aria-label") || target.title || target.innerText?.trim();
    if (label && !/parar e salvar|iniciar demonstração/i.test(label)) wb.recordedSteps.push(`Acionar: ${label.replace(/\s+/g, " ").slice(0, 300)}`);
  }

  document.addEventListener("click", recordAction, true);
  document.getElementById("workbench-close")?.addEventListener("click", () => dialog.close());
  document.getElementById("workbench-mobile-launch")?.addEventListener("click", () => window.openDexWorkbench());
  document.getElementById("workbench-voice")?.addEventListener("click", toggleVoice);
  document.getElementById("workbench-appshot")?.addEventListener("click", () => captureAppshot().catch(error => toast(error.message, "error")));

  dialog?.addEventListener("click", async event => {
    try {
      const tab = event.target.closest("[data-workbench-tab]")?.dataset.workbenchTab;
      const open = event.target.closest("[data-workbench-open]")?.dataset.workbenchOpen;
      if (tab || open) return await selectTab(tab || open);
      if (event.target.closest("[data-workbench-read-last]")) return readLastResponse();
      if (event.target.closest("[data-workbench-appshot]")) return await captureAppshot();
      if (event.target.closest("[data-artifact-refresh]")) { wb.artifactQuery = document.getElementById("artifact-search")?.value.trim() || ""; await loadArtifacts(); return render(); }
      const artifact = event.target.closest("[data-artifact-preview]");
      if (artifact) return await previewArtifact(artifact.dataset.artifactPreview, artifact.dataset.artifactKind);
      const annotate = event.target.closest("[data-artifact-annotate]")?.dataset.artifactAnnotate;
      if (annotate) { const body = window.prompt(`Anotação para ${annotate}:`); const page = window.prompt("Página (opcional):", ""); if (body?.trim()) await api(endpoint("/artifacts/annotations"), {method:"POST", body:JSON.stringify({path:annotate, body:body.trim(), page:page ? Number(page) : null})}); await loadArtifacts(); return render(); }
      const deleteAnnotation = event.target.closest("[data-artifact-delete-annotation]")?.dataset.artifactDeleteAnnotation;
      if (deleteAnnotation) { await api(endpoint(`/artifacts/annotations/${encodeURIComponent(deleteAnnotation)}`), {method:"DELETE"}); await loadArtifacts(); return render(); }
      if (event.target.closest("[data-artifact-compare-selected]")) return await compareArtifacts();
      const diff = event.target.closest("[data-git-diff]")?.dataset.gitDiff;
      if (diff) return await showDiff(diff);
      if (event.target.closest("[data-git-refresh]")) { await loadGit(); return render(); }
      const action = event.target.closest("[data-git-action]")?.dataset.gitAction;
      if (action) return await gitAction(action);
      if (event.target.closest("[data-git-add-comment]")) {
        const payload = {repo:wb.git.repo, path:document.getElementById("git-comment-path").value.trim(), line:Number(document.getElementById("git-comment-line").value), body:document.getElementById("git-comment-body").value.trim()};
        await api(endpoint("/git/comments"), {method:"POST", body:JSON.stringify(payload)}); await loadGit(); return render();
      }
      const deleteComment = event.target.closest("[data-git-delete-comment]")?.dataset.gitDeleteComment;
      if (deleteComment) { await api(endpoint(`/git/comments/${encodeURIComponent(deleteComment)}`), {method:"DELETE"}); await loadGit(); return render(); }
      if (event.target.closest("[data-worktree-create]")) {
        const payload = {repo:wb.git.repo, name:document.getElementById("worktree-name").value.trim(), branch:document.getElementById("worktree-branch").value.trim(), base:document.getElementById("worktree-base").value.trim() || "HEAD"};
        const result = await api(endpoint("/worktrees"), {method:"POST", body:JSON.stringify(payload)}); toast(`Worktree criado em ${result.path}`, "success"); await loadWorktrees(); return render();
      }
      const removeWorktree = event.target.closest("[data-worktree-remove]")?.dataset.worktreeRemove;
      if (removeWorktree && window.confirm("Remover este worktree? A operação será recusada se houver mudanças não integradas.")) { await api(endpoint("/worktrees"), {method:"DELETE", body:JSON.stringify({repo:wb.git.repo, path:removeWorktree, confirm:true})}); await loadWorktrees(); return render(); }
      if (event.target.closest("[data-memory-add]")) { const text = document.getElementById("memory-text").value.trim(); const tags = document.getElementById("memory-tags").value.split(",").map(value => value.trim()).filter(Boolean); await api(endpoint("/memory"), {method:"POST", body:JSON.stringify({text, tags})}); await loadMemory(); return render(); }
      const deleteMemory = event.target.closest("[data-memory-delete]")?.dataset.memoryDelete;
      if (deleteMemory) { await api(endpoint(`/memory/${encodeURIComponent(deleteMemory)}`), {method:"DELETE"}); await loadMemory(); return render(); }
      if (event.target.closest("[data-playbook-record]")) {
        if (!wb.recording) { wb.recordedSteps = []; wb.recording = true; dialog.close(); toast("Demonstração iniciada. Abra a Central novamente para parar e salvar."); return; }
        wb.recording = false;
        const name = window.prompt("Nome desta rotina:", "Nova rotina demonstrada");
        if (name && wb.recordedSteps.length) await api(endpoint("/playbooks"), {method:"POST", body:JSON.stringify({name, steps:wb.recordedSteps, notes:"Capturada na interface do Dex"})});
        await loadPlaybooks(); return render();
      }
      const replay = event.target.closest("[data-playbook-replay]")?.dataset.playbookReplay;
      if (replay) { const item = wb.playbooks.find(playbook => playbook.id === replay); if (item) { window.dexSetPrompt?.(`Execute a rotina demonstrada \"${item.name}\". Revise o estado atual antes de cada ação, use somente mecanismos auditáveis e mantenha as aprovações normais.\n\nPassos demonstrados:\n${item.steps.map((step, index) => `${index + 1}. ${step}`).join("\n")}\n\nObservações: ${item.notes || "nenhuma"}`); dialog.close(); toast("Rotina colocada no compositor para revisão.", "success"); } return; }
      const deletePlaybook = event.target.closest("[data-playbook-delete]")?.dataset.playbookDelete;
      if (deletePlaybook) { await api(endpoint(`/playbooks/${encodeURIComponent(deletePlaybook)}`), {method:"DELETE"}); await loadPlaybooks(); return render(); }
    } catch (error) { toast(error.message, "error"); }
  });

  dialog?.addEventListener("change", async event => {
    try {
      if (event.target.id === "git-repo") { wb.git.repo = event.target.value; await loadGit(); render(); }
      if (event.target.matches("[data-artifact-compare]")) { if (event.target.checked) wb.compare.add(event.target.dataset.artifactCompare); else wb.compare.delete(event.target.dataset.artifactCompare); if (wb.compare.size > 2) { wb.compare.delete([...wb.compare][0]); } render(); }
      if (event.target.id === "memory-enabled") { await api(endpoint("/memory/config"), {method:"PUT", body:JSON.stringify({enabled:event.target.checked})}); wb.memory.enabled = event.target.checked; render(); }
    } catch (error) { toast(error.message, "error"); }
  });
})();
