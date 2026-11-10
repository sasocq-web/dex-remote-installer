/* Conversation workspace. No network work at startup; all history/file reads are explicit. */
(() => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const ctx = () => window.dexConversationContext?.() || {};
  const api = (...args) => window.dexApi(...args);
  const notify = message => window.dexToast?.(message, 'error');
  const readLocal = (key, fallback) => {try {return JSON.parse(localStorage.getItem(key)) ?? fallback;} catch {return fallback;}};
  const writeLocal = (key, value) => {try {localStorage.setItem(key, JSON.stringify(value));return true;} catch {notify('Armazenamento local indisponível; seu texto continua no editor.');return false;}};
  const date = value => new Date(value * 1000).toLocaleString('pt-BR');
  const root = document.createElement('nav');
  root.className = 'conversation-tools'; root.setAttribute('aria-label','Ferramentas da conversa');
  root.innerHTML = [['search','Buscar'],['index','Índice'],['editor','Ampliar editor'],['changes','Alterações'],['library','Resultados'],['context','Contexto']].map(([action,label])=>`<button type="button" data-cw-open="${action}">${label}</button>`).join('')+'<button type="button" data-cw-return>Mais recente</button>';
  const menu = document.createElement('details');
  menu.className = 'conversation-tools-menu'; menu.hidden = true;
  menu.innerHTML = '<summary aria-label="Ferramentas da conversa" title="Ferramentas da conversa"><span aria-hidden="true">⋯</span><span class="cw-menu-label">Conversa</span></summary>';
  root.id = 'conversation-tools-strip'; root.hidden = true;
  document.querySelector('.topbar')?.after(root);
  menu.querySelector('summary').setAttribute('aria-controls', root.id);
  (document.querySelector('.topbar-actions') || document.querySelector('.topbar'))?.prepend(menu);
  let menuThread = '';
  function syncToolStrip() {
    root.hidden = menu.hidden || !menu.open;
    menu.querySelector('summary').setAttribute('aria-expanded', String(!root.hidden));
  }
  function syncVisibility() {
    const current = window.dexConversationIdentity?.() || {threadId:ctx().threadId};
    const id = current.threadId || '';
    if (!id || id !== menuThread) {menu.open = false; root.scrollLeft = 0;}
    menu.hidden = !id; menuThread = id;
    syncToolStrip();
  }
  menu.addEventListener('toggle', syncToolStrip);
  // An inline strip stays open while reading or choosing tools. The header
  // disclosure and Escape collapse it; switching conversation resets it.
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menu.open && !document.querySelector('dialog[open]')) {
      menu.open = false; syncToolStrip(); menu.querySelector('summary').focus();
    }
  });
  const dialog = document.createElement('dialog');
  dialog.className = 'conversation-workspace'; dialog.setAttribute('aria-labelledby','cw-title');
  dialog.innerHTML = '<header><h2 id="cw-title"></h2><button type="button" data-cw-close aria-label="Fechar painel">×</button></header><div class="cw-body"></div>';
  document.body.append(dialog);
  const body = dialog.querySelector('.cw-body');
  let view = '', generation = 0, indexRequest = 0, bound = null, records = [], bookmarksOnly = false;
  let editorBase = '', editorKey = '', side = null, sideTimer = null, selection = '', selectionItem = '';
  const seen = readLocal('dex-attention-seen-v1', {});
  const overview = document.createElement('section');
  overview.className = 'cw-overview inspector-section';
  overview.setAttribute('aria-labelledby','cw-overview-title');
  overview.innerHTML = `<div class="cw-overview-heading"><h3 id="cw-overview-title">Todas as conversas</h3><p>Acompanhe atividades e ajuste sua leitura.</p></div>
    <details class="cw-overview-attention"><summary><span>Atenção geral</span><span class="badge cw-overview-count">0</span></summary><p class="cw-overview-stats" role="status"></p><div class="cw-overview-list"></div><button type="button" data-cw-open="attention">Ver lista completa</button></details>
    <details class="cw-overview-reading"><summary>Leitura e aparência</summary><div class="cw-reading-controls"></div></details>`;
  const overviewHost = document.querySelector('#tab-summary .panel-content');
  overviewHost?.prepend(overview);
  overview.querySelector('.cw-overview-attention').addEventListener('toggle',()=>refreshAttentionViews());
  overview.querySelector('.cw-overview-reading').addEventListener('toggle',event=>{if(event.target.open)renderReadingControls(overview.querySelector('.cw-reading-controls'));});
  // Reading preferences remain reachable when focus mode hides the inspector.
  document.addEventListener('click',event=>{
    if (!document.body.classList.contains('cw-focus') || !event.target.closest('#open-inspector')) return;
    event.preventDefault();event.stopImmediatePropagation();
    writeLocal('dex-reading-v1',applyReading({...readLocal('dex-reading-v1',{}),focus:false}));
    document.querySelectorAll('.cw-reading-form [name=focus]').forEach(input=>{input.checked=false;});
    window.dexOpenOverview?.();
  },true);

  const endpoint = (suffix, context = bound || ctx()) => {
    if (!context.project?.id || !context.threadId) throw new Error('Abra uma conversa para usar este recurso.');
    return `/api/conversation/${encodeURIComponent(context.project.id)}/threads/${encodeURIComponent(context.threadId)}${suffix}`;
  };
  const versionEndpoint = (suffix = '', context = bound) => `/api/conversation/${encodeURIComponent(context.project.id)}/versions${suffix}`;
  const post = (url, payload) => api(url, {method:'POST', body:JSON.stringify(payload)});
  const button = (action, label, data='') => `<button type="button" data-cw-action="${action}" ${data}>${label}</button>`;
  const errorHTML = error => `<p class="cw-error" role="alert">${esc(error.message || error)}</p>`;
  const draftKey = context => 'dex-expanded-drafts-v1:' + context.project?.id + ':' + (context.threadId || 'new');
  const draftVersions = () => readLocal(editorKey, []);
  function snapshotDraft(text) {
    const values = draftVersions();
    if (!text || values.some(value=>value.text===text)) return true;
    values.unshift({text, at:Date.now()/1000});
    // Bound storage, retaining the newest versions without changing the main draft.
    let size = 0;
    return writeLocal(editorKey, values.filter((item,i) => {size += item.text.length;return i < 12 && size <= 1_000_000;}));
  }
  function draftOptions() {
    return '<option value="">Selecione uma versão</option>'+draftVersions().map(v=>`<option value="${v.at}">${esc(date(v.at))} · ${v.text.length} caracteres</option>`).join('');
  }
  function stopSideTimer() {clearTimeout(sideTimer);sideTimer=null;}
  function show(title, name) {
    generation++; stopSideTimer(); view=name; bound=ctx();
    document.getElementById('cw-title').textContent=title;
    body.innerHTML='<p role="status">Carregando…</p>';
    if (!dialog.open) dialog.showModal();
    return generation;
  }
  function close() {
    if (view === 'editor') {
      const value=body.querySelector('#cw-editor')?.value;
      if (value && !snapshotDraft(value)) return;
    }
    dialog.close(); generation++; stopSideTimer(); view='';
  }
  dialog.addEventListener('cancel', event => {event.preventDefault();close();});
  root.querySelector('[data-cw-return]').addEventListener('click',()=>window.dexConversationReturnLive?.());
  document.addEventListener('selectionchange', () => {
    const selected=window.getSelection();
    const card=selected?.anchorNode?.parentElement?.closest('#message-list [data-item-id]');
    if (card&&String(selected).trim()) {selection=String(selected).slice(0,12000);selectionItem=card.dataset.itemId;}
  });

  function highlight(text, query) {
    if (!query) return esc(text);
    const i=String(text).toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
    if(i<0) return esc(text);
    return esc(text.slice(0,i))+'<mark>'+esc(text.slice(i,i+query.length))+'</mark>'+esc(text.slice(i+query.length));
  }
  function indexRows() {
    const filtered=bookmarksOnly ? records.filter(r=>r.bookmark) : records;
    return filtered.map(r=>`<article class="cw-result"><div class="cw-row">${button('jump',esc(r.bookmark?.label || (r.request?'Pedido':r.final?'Resposta final':'Mensagem')),`data-id="${esc(r.id)}"`)}${button('bookmark',r.bookmark?'★ Remover marcador':'☆ Marcar',`data-id="${esc(r.id)}"`)}${r.completed?button('branch','Ramificar',`data-id="${esc(r.id)}"`):''}</div><p>${highlight(r.preview,r.match)}</p></article>`).join('') || '<p>Nenhuma mensagem encontrada.</p>';
  }
  async function loadIndex(query='', append=false) {
    const ticket=generation, request=++indexRequest, context=bound;
    const offset=append?Number(body.querySelector('[data-cw-action="more"]')?.dataset.offset || 0):0;
    const data=await api(endpoint('/index?'+new URLSearchParams({q:query,offset,bookmarks_only:bookmarksOnly}),context));
    if(ticket!==generation||request!==indexRequest) return;
    records=append?[...records,...data.items]:data.items;
    body.querySelector('.cw-results').innerHTML=indexRows();
    body.querySelector('.cw-count').textContent=`${records.length} de ${data.total} mensagens`;
    const more=body.querySelector('.cw-more');
    more.innerHTML=data.next_offset!=null?button('more','Carregar mais',`data-offset="${data.next_offset}"`):'';
    const source=body.querySelector('.cw-source');
    source.innerHTML=data.lineage?button('source','↗ Conversa de origem',`data-id="${esc(data.lineage.thread_id)}" data-item="${esc(data.lineage.item_id)}"`):'';
  }
  async function openIndex(search=false) {
    show(search?'Buscar nesta conversa':'Índice e marcadores',search?'search':'index'); records=[];bookmarksOnly=false;
    endpoint('');
    body.innerHTML=`<form class="cw-search"><input type="search" id="cw-query" aria-label="Buscar em todo o histórico" maxlength="500" placeholder="Buscar em todo o histórico"><button type="submit">Buscar</button></form><div class="cw-row"><label><input type="checkbox" id="cw-only-bookmarks"> Somente marcadores</label><span class="cw-count" role="status"></span></div><p>Ramificar copia a conversa até o fim do turno selecionado. Os arquivos do projeto continuam compartilhados.</p><div class="cw-source"></div><div class="cw-results"></div><div class="cw-more"></div>`;
    body.querySelector('form').addEventListener('submit',event=>{event.preventDefault();loadIndex(body.querySelector('#cw-query').value).catch(error=>notify(error.message));});
    body.querySelector('#cw-only-bookmarks').addEventListener('change',event=>{bookmarksOnly=event.target.checked;loadIndex(body.querySelector('#cw-query').value).catch(error=>notify(error.message));});
    await loadIndex();body.querySelector('#cw-query')?.focus();
  }
  function attachmentHTML(context) {
    return `<h3>Anexos e referências (${context.references?.length||0})</h3>`+(context.references||[]).map(r=>`<p>${esc(r.name||r.path||r.id||r.type)}</p>`).join('') || '<p>Nenhum anexo.</p>';
  }
  function openEditor() {
    show('Editor ampliado e versões do rascunho','editor');
    editorBase=bound.draft||''; editorKey=draftKey(bound);snapshotDraft(editorBase);
    body.innerHTML=`<p>Aplicar atualiza o rascunho da conversa. O envio continua pelo botão habitual.</p><textarea id="cw-editor" aria-label="Editor ampliado" maxlength="200000"></textarea><div class="cw-row">${button('save-draft','Guardar versão')}${button('apply-draft','Aplicar ao rascunho')}</div><label>Versões locais <select id="cw-draft-versions">${draftOptions()}</select></label><details><summary>Revisar anexos e referências</summary>${attachmentHTML(bound)}</details><p class="cw-editor-status" role="status"></p>`;
    body.querySelector('#cw-editor').value=editorBase;
    body.querySelector('#cw-editor').focus();
    body.querySelector('#cw-draft-versions').addEventListener('change',event=>{
      if(event.target.value==='')return;
      const selected=draftVersions().find(value=>String(value.at)===event.target.value)?.text;
      snapshotDraft(body.querySelector('#cw-editor').value);
      if(selected!==undefined)body.querySelector('#cw-editor').value=selected;
      event.target.innerHTML=draftOptions();
    });
  }
  function applyDraft() {
    const current=ctx(), value=body.querySelector('#cw-editor').value;
    if(current.threadId!==bound.threadId||current.project?.id!==bound.project?.id) throw new Error('A conversa mudou. O texto foi mantido no editor; volte à origem para aplicar.');
    if(current.draft!==editorBase) throw new Error('O rascunho principal mudou enquanto o editor estava aberto. Guarde esta versão e reabra o editor para comparar.');
    if(!snapshotDraft(value))return;
    window.dexSetPrompt(value); editorBase=value; close();
  }

  function diffHTML(diff, defaultPath='') {
    let line=0, old=0, path=defaultPath;
    return '<div class="cw-diff" role="region" aria-label="Diferenças de código">'+String(diff).split('\n').slice(0,25000).map(text=>{
      if(text.startsWith('+++ ')&&!defaultPath) path=text.slice(4).replace(/^b\//,'').split('\t')[0];
      const hunk=text.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)/);
      if(hunk){old=Number(hunk[1]);line=Number(hunk[2]);}
      const header=/^(diff |index |--- |\+\+\+ |@@)/.test(text);
      const added=!header&&text.startsWith('+'), removed=!header&&text.startsWith('-');
      const number=!header&&!text.startsWith('\\')?(removed?old++:line++):0;
      if(!header&&!added&&!removed&&number)old++;
      return `<div class="cw-diff-line ${added?'cw-add':removed?'cw-del':header?'cw-hunk':''}">${number&&path&&path!=='/dev/null'?`<button type="button" aria-label="Comentar linha ${number}" title="Comentar linha ${number}" data-cw-action="comment" data-path="${esc(path)}" data-line="${number}" data-side="${removed?'old':'new'}">${number}</button>`:'<span></span>'}<code>${esc(text)}</code></div>`;
    }).join('')+'</div>';
  }
  async function openChanges() {
    const ticket=show('Alterações do último turno','changes');
    const [data,review]=await Promise.all([api(endpoint('/changes')),api(endpoint('/review-comments'))]);
    if(ticket!==generation)return;
    body.dataset.turnId=data.turn_id||'';
    body.innerHTML=`<p>Diferenças registradas nas operações de arquivo do turno ${esc(data.turn_id||'—')}. Comandos de terminal podem não registrar diferenças aqui.</p>`+data.changes.map(c=>`<section><h3>${esc(c.path)}</h3>${c.diff?diffHTML(c.diff,c.path):'<p>Operação sem diff textual disponível.</p>'}</section>`).join('')+(data.changes.length?'':'<p>Nenhuma operação de arquivo registrada neste turno.</p>')+(data.truncated?'<p>Visualização limitada por tamanho.</p>':'')+'<h3>Comentários guardados</h3><div class="cw-comment-area">'+(review.comments||[]).filter(c=>c.turn_id===data.turn_id).map(c=>`<p><strong>${esc(c.path)}:${c.line} (${c.side==='old'?'anterior':'nova'})</strong> ${esc(c.body)}</p>`).join('')+'</div>';
  }
  function commentForm(target, project, repo='.', conversation=null) {
    const turnId=conversation?body.dataset.turnId:null;
    const area=document.createElement('form');area.className='cw-inline-comment';
    area.innerHTML=`<strong>${esc(target.dataset.path)}:${esc(target.dataset.line)} · ${target.dataset.side==='old'?'versão anterior':'versão nova'}</strong><textarea aria-label="Comentário da linha" required maxlength="20000"></textarea><button type="submit">Guardar comentário</button><button type="button" data-cw-cancel-comment>Cancelar</button><span role="status"></span>`;
    target.closest('.cw-diff-line').after(area);area.querySelector('textarea').focus();
    area.querySelector('[data-cw-cancel-comment]').onclick=()=>area.remove();
    area.onsubmit=async event=>{event.preventDefault();const submit=area.querySelector('[type="submit"]');submit.disabled=true;
      try{
        const text=area.querySelector('textarea').value;
        if(conversation)await post(endpoint('/review-comments',conversation),{turn_id:turnId,path:target.dataset.path,line:Number(target.dataset.line),side:target.dataset.side,body:text});
        else await post(`/api/workbench/${encodeURIComponent(project)}/git/comments`,{repo,path:target.dataset.path,line:Number(target.dataset.line),body:`[${target.dataset.side==='old'?'anterior':'nova'}] ${text}`});
        area.querySelector('[role="status"]').textContent='Comentário guardado';
      }
      catch(error){notify(error.message);submit.disabled=false;}
    };
  }
  async function openLibrary() {
    const ticket=show('Resultados e versões da conversa','library');endpoint('');
    const [files,versions]=await Promise.all([api(`/api/workbench/${encodeURIComponent(bound.project.id)}/artifacts?limit=200`),api(versionEndpoint('?thread_id='+encodeURIComponent(bound.threadId)))]);
    if(ticket!==generation)return;
    const items=files.artifacts||files.items||[];
    body.innerHTML=`<p>Associe um arquivo do projeto a esta conversa guardando uma versão. Versões guardadas são cópias imutáveis e incluem data e SHA-256.</p><label>Arquivo <select id="cw-file">${items.map(f=>`<option value="${esc(f.path)}">${esc(f.path)}</option>`).join('')}</select></label>${button('snapshot','Guardar versão nesta conversa')}<h3>Versões guardadas</h3><div class="cw-versions">${versions.versions.map(v=>`<label class="cw-version"><input type="checkbox" data-cw-version="${esc(v.id)}" data-path="${esc(v.path)}"> <strong>${esc(v.path)}</strong> · ${esc(date(v.created_at))} · ${v.size} B <a href="${versionEndpoint('/'+encodeURIComponent(v.id))}" download>Baixar</a><small>SHA-256 ${esc(v.sha256)}</small></label>`).join('')||'<p>Ainda não há versões associadas a esta conversa.</p>'}</div>${button('compare','Comparar duas versões de texto')}<div class="cw-comparison"></div>`;
  }
  async function compareVersions() {
    const selected=[...body.querySelectorAll('[data-cw-version]:checked')];
    if(selected.length!==2)throw new Error('Selecione exatamente duas versões de texto.');
    if(selected[0].dataset.path!==selected[1].dataset.path)throw new Error('Selecione duas versões do mesmo arquivo.');
    const ticket=generation, values=await Promise.all(selected.map(el=>api(versionEndpoint('/'+encodeURIComponent(el.dataset.cwVersion)+'?text=true'))));
    if(ticket!==generation)return;
    const lines=values.map(value=>String(value).split('\n').slice(0,10000));
    body.querySelector('.cw-comparison').innerHTML=`<h3>${esc(selected[0].dataset.path)}</h3><p>Linhas que diferem na mesma posição estão realçadas. Prévia limitada a 10.000 linhas por versão.</p><div class="cw-compare-grid">${lines.map((value,i)=>`<section><h4>Versão ${i+1}</h4><pre>${value.map((line,n)=>`<span class="${line!==lines[1-i][n]?(i?'cw-add':'cw-del'):''}">${esc(line)||' '}</span>`).join('\n')}</pre></section>`).join('')}</div>`;
  }
  async function openContext() {
    const ticket=show('Contexto enviado e recursos associados','context');const data=await api(endpoint('/context'));
    if(ticket!==generation)return;
    body.innerHTML=`<p>O registro mostra as opções efetivamente enviadas pelo Dex a partir desta atualização. Recursos disponíveis não significam que foram usados ou lidos pelo modelo.</p><h3>Recursos associados agora</h3><pre>${esc(JSON.stringify({project:bound.project,tools:data.profile,references:bound.references},null,2))}</pre><h3>Envios registrados</h3>`+Object.entries(data.turns).reverse().map(([id,value])=>`<details><summary>${esc(date(value.sent_at))} · ${esc(id)}</summary><pre>${esc(JSON.stringify(value,null,2))}</pre></details>`).join('')+(Object.keys(data.turns).length?'':'<p>Esta conversa ainda não tem envios registrados pela nova versão.</p>');
  }
  const overviewThreads = () => window.dexConversationOverview?.() || ctx().threads || [];
  function attentionThreads() {
    const rank = {waiting:0,failed:1,active:2};
    return overviewThreads().filter(t=>['waiting','active','failed'].includes(t.uiStatus)||Number(t.updatedAt||0)>Number(seen[t.id]||0))
      .sort((a,b)=>(rank[a.uiStatus]??3)-(rank[b.uiStatus]??3)||Number(b.updatedAt||0)-Number(a.updatedAt||0));
  }
  function attentionRows(threads=attentionThreads(),limit=Infinity) {
    const labels={waiting:'Aguardando sua ação',active:'Em execução',failed:'Falha',completed:'Concluída'};
    return threads.slice(0,limit).map(t=>`<article class="cw-result"><span class="cw-status ${esc(t.uiStatus)}">${labels[t.uiStatus]||(t.clc?.execution_timing?.turn_count?'Concluída':'Atualizada')}</span>${button('attention-open',esc(t.title||t.name||t.preview||'Conversa').slice(0,160),`data-id="${esc(t.id)}"`)}${button('attention-read','Marcar visto',`data-id="${esc(t.id)}" data-updated="${Number(t.updatedAt||0)}"`)}</article>`).join('')||'<p>Nenhuma pendência entre as conversas carregadas.</p>';
  }
  function refreshAttentionViews() {
    const threads=attentionThreads();
    overview.querySelector('.cw-overview-count').textContent=threads.length>99?'99+':String(threads.length);
    overview.querySelector('.cw-overview-count').setAttribute('aria-label',`${threads.length} conversas com atividade`);
    const counts={waiting:0,failed:0,active:0};for(const thread of threads)if(thread.uiStatus in counts)counts[thread.uiStatus]++;
    overview.querySelector('.cw-overview-stats').textContent=`${counts.waiting} aguardando sua ação · ${counts.active} em execução · ${counts.failed} com falha`;
    if(overview.querySelector('.cw-overview-attention').open)overview.querySelector('.cw-overview-list').innerHTML=attentionRows(threads,8);
    if(view==='attention'&&dialog.open)body.querySelector('.cw-attention').innerHTML=attentionRows(threads);
  }
  function openAttention() {show('Central de atenção','attention');body.innerHTML='<p>Execuções, pedidos de resposta/aprovação, falhas e conversas atualizadas. Abra uma conversa para responder usando o fluxo protegido habitual.</p><div class="cw-attention">'+attentionRows()+'</div>';}
  function applyReading(value) {
    const prefs={font:Math.min(22,Math.max(13,Number(value.font)||16)),width:Math.min(1200,Math.max(600,Number(value.width)||900)),spacing:Math.min(2.2,Math.max(1.3,Number(value.spacing)||1.65)),dense:!!value.dense,focus:!!value.focus};
    document.documentElement.style.setProperty('--dex-font-scale',prefs.font/16);document.documentElement.style.setProperty('--cw-font','1rem');document.documentElement.style.setProperty('--cw-width',prefs.width+'px');document.documentElement.style.setProperty('--cw-spacing',prefs.spacing);
    document.body.classList.toggle('cw-dense',prefs.dense);document.body.classList.toggle('cw-focus',prefs.focus);
    return prefs;
  }
  applyReading(readLocal('dex-reading-v1',{}));
  function renderReadingControls(container) {
    const prefs=applyReading(readLocal('dex-reading-v1',{}));
    container.innerHTML=`<form class="cw-reading-form"><label>Tamanho dos textos do Dex <output data-cw-font-size>${Math.round(prefs.font/16*100)}%</output><input aria-label="Tamanho dos textos do Dex" name="font" type="range" min="13" max="22" value="${prefs.font}"></label><label>Largura da conversa <input name="width" type="range" min="600" max="1200" step="50" value="${prefs.width}"></label><label>Espaçamento <input name="spacing" type="range" min="1.3" max="2.2" step=".05" value="${prefs.spacing}"></label><label><input name="dense" type="checkbox" ${prefs.dense?'checked':''}> Leitura compacta</label><label><input name="focus" type="checkbox" ${prefs.focus?'checked':''}> Modo foco</label><button type="button" data-cw-reading-reset>Restaurar padrão</button></form><p>O tamanho ajusta proporcionalmente os textos de toda a interface. Preferências salvas neste navegador. No modo foco, use o botão do painel direito para voltar.</p>`;
    container.querySelector('form').addEventListener('submit',event=>event.preventDefault());
    container.querySelector('form').addEventListener('input',event=>{const form=event.currentTarget;const prefs=applyReading({font:form.elements.font.value,width:form.elements.width.value,spacing:form.elements.spacing.value,dense:form.elements.dense.checked,focus:form.elements.focus.checked});writeLocal('dex-reading-v1',prefs);document.querySelectorAll('.cw-reading-form').forEach(other=>{other.elements.font.value=prefs.font;other.querySelector('[data-cw-font-size]').textContent=Math.round(prefs.font/16*100)+'%';});});
    container.querySelector('[data-cw-reading-reset]').addEventListener('click',()=>{writeLocal('dex-reading-v1',{});applyReading({});renderReadingControls(container);});
  }
  function openReading() {show('Preferências de leitura','reading');renderReadingControls(body);}
  async function branch(record) {
    if(!record?.completed)throw new Error('Escolha uma mensagem de um turno concluído.');
    const ticket=generation, context=bound;
    const result=await post(endpoint('/branch'),{turn_id:record.turn_id,item_id:record.id,key:record.branchKey ||= crypto.randomUUID()});
    const id=(result.thread||result.forkedThread)?.id;if(!id)throw new Error('Conversa não retornada.');
    if(ticket!==generation)return;
    close();await window.dexConversationNavigate(id,context.project.id);
  }
  function renderSide() {
    if(view!=='side'||!side)return;
    const log=body.querySelector('.cw-side-log');if(!log)return;
    const items=(side.thread?.turns||[]).flatMap(t=>t.items||[]).filter(i=>['userMessage','agentMessage','assistantMessage'].includes(i.type));
    const text=i=>i.text||i.message||(Array.isArray(i.content)?i.content.map(p=>p.text||'').join('\n'):i.content)||'';
    window.DexRichContent?.beforeRender(log);
    log.innerHTML=items.map(i=>{
      const value=text(i),user=i.type==='userMessage';
      const content=user?esc(value.includes('Solicitação atual:\n')?value.split('Solicitação atual:\n').at(-1):value):(window.DexRichContent?.render(value,'side-'+i.id)||esc(value));
      return `<article data-item-id="side-${esc(i.id)}" class="message-card ${user?'user':'assistant'}"><strong>${user?'Você':'Codex'}</strong><div class="message-body">${content}</div></article>`;
    }).join('');
    window.DexRichContent?.enhance(log);
    log.querySelectorAll('[data-rich-action="document"]').forEach(button=>button.remove());
    body.querySelector('.cw-side-status').textContent=side.running?'Em execução…':'Pronta para continuar';
    body.querySelector('[type="submit"]').disabled=side.running;
  }
  async function refreshSide() {
    if(view!=='side'||!dialog.open||!side?.id)return;
    const current=side, ticket=generation;
    try{const data=await api(`/api/threads/${encodeURIComponent(current.id)}?`+new URLSearchParams({project_id:current.projectId,item_limit:200}));
      if(ticket!==generation||side!==current)return;
      current.thread=data.thread||data;current.running=(current.thread.turns||[]).some(t=>t.status==='inProgress');renderSide();
    }catch(error){if(ticket===generation)notify(error.message);}
    if(ticket===generation&&current.running)sideTimer=setTimeout(refreshSide,3000);
  }
  function openSide(itemId) {
    const origin=ctx();const item=origin.items?.find(i=>i.id===itemId);
    const quote=(selectionItem===itemId?selection:'')||item?.text||'';selection='';selectionItem='';
    show('Conversa lateral','side');
    side={id:null,projectId:origin.project.id,origin:origin.threadId,item:itemId,quote:String(quote).slice(0,12000),running:false};
    body.innerHTML=`<p>Uma conversa separada preserva o andamento da origem. O trecho selecionado será incluído no primeiro envio.</p><details open><summary>Trecho de origem</summary><blockquote>${esc(side.quote)}</blockquote></details><div class="cw-side-log"></div><p class="cw-side-status" role="status"></p><form class="cw-side-form"><textarea aria-label="Pergunta lateral" maxlength="100000" required></textarea><button type="submit">Enviar na conversa lateral</button></form><div class="cw-side-links"></div>`;
    body.querySelector('.cw-side-log').addEventListener('click',event=>{
      const action=event.target.closest('[data-rich-action="quote"]');if(!action)return;
      event.preventDefault();event.stopPropagation();
      const text=String(window.getSelection()).trim()||action.closest('.message-card')?.querySelector('.dex-prose')?.textContent||'';
      const input=body.querySelector('.cw-side-form textarea');
      input.value+=(input.value?'\n\n':'')+'Sobre este trecho da conversa lateral:\n\n'+text.slice(0,12000)+'\n\n';input.focus();
    });
    body.querySelector('form').onsubmit=async event=>{event.preventDefault();const current=side, input=event.currentTarget.querySelector('textarea'), question=input.value;if(!question.trim()||current.running)return;current.running=true;renderSide();
      try {
        const message=current.id?question:`Conversa lateral sobre um trecho. Responda à pergunta abaixo; a tarefa da conversa de origem continua separadamente.\nOrigem: ${current.origin} · mensagem ${current.item}\n\nTrecho:\n${current.quote}\n\nPergunta:\n${question}`;
        const payload={...origin.payload,message,references:[]};delete payload.goal_mode;payload.collaboration_mode=null;
        if(!current.id){
          if(current.creationAttempted)throw new Error('A criação anterior ficou sem confirmação. Confira a lista de conversas antes de criar outra.');
          current.creationAttempted=true;
          const result=await post('/api/threads',{...payload,message:null});current.id=(result.thread||result)?.id;
          if(!current.id)throw new Error('A conversa lateral não retornou um identificador.');
        }
        await post(`/api/threads/${encodeURIComponent(current.id)}/messages`,payload);
        if(side!==current)return;
        input.value='';body.querySelector('.cw-side-links').innerHTML=button('side-open','Abrir conversa lateral completa');await refreshSide();
      }catch(error){current.running=false;renderSide();notify(error.message);}
    };
    body.querySelector('textarea').focus();
  }
  async function open(name) {
    syncVisibility();
    if(menu.hidden&&!['attention','reading'].includes(name)) return;
    try {
      if(name==='search'||name==='index')return await openIndex(name==='search');
      if(name==='editor')return openEditor();
      if(name==='changes')return await openChanges();
      if(name==='library')return await openLibrary();
      if(name==='context')return await openContext();
      if(name==='attention')return openAttention();
      if(name==='reading')return openReading();
    }catch(error){body.innerHTML=errorHTML(error);}
  }
  document.addEventListener('click',async event=>{
    const target=event.target.closest('[data-cw-open],[data-cw-action],[data-cw-close],[data-cw-side]');if(!target)return;
    if(target.hasAttribute('data-cw-close'))return close();
    if(target.dataset.cwOpen)return open(target.dataset.cwOpen);
    if(target.dataset.cwSide)return openSide(target.dataset.cwSide);
    const action=target.dataset.cwAction;target.disabled=true;
    try {
      if(action==='jump'){const context=bound, id=target.dataset.id,query=body.querySelector('#cw-query')?.value||'';close();await window.dexConversationJump(id,query,context);}
      if(action==='source'){const context=bound;close();await window.dexConversationNavigate(target.dataset.id,context.project.id);await window.dexConversationJump(target.dataset.item,'',ctx());}
      if(action==='bookmark'){const record=records.find(r=>r.id===target.dataset.id);await post(endpoint('/bookmark'),{item_id:record.id,enabled:!record.bookmark,label:record.preview.slice(0,80)});await loadIndex(body.querySelector('#cw-query').value);}
      if(action==='branch')await branch(records.find(r=>r.id===target.dataset.id));
      if(action==='more')await loadIndex(body.querySelector('#cw-query').value,true);
      if(action==='save-draft'){if(snapshotDraft(body.querySelector('#cw-editor').value)){body.querySelector('.cw-editor-status').textContent='Versão guardada neste navegador';body.querySelector('#cw-draft-versions').innerHTML=draftOptions();}}
      if(action==='apply-draft')applyDraft();
      if(action==='snapshot'){const path=body.querySelector('#cw-file')?.value;if(!path)throw new Error('Escolha um arquivo.');await post(versionEndpoint(),{path,thread_id:bound.threadId});await openLibrary();}
      if(action==='compare')await compareVersions();
      if(action==='comment')commentForm(target,bound.project.id,'.',bound);
      if(action==='reset-reading'){writeLocal('dex-reading-v1',{});applyReading({});openReading();}
      if(action==='attention-read'){seen[target.dataset.id]=Number(target.dataset.updated);writeLocal('dex-attention-seen-v1',seen);refreshAttentionViews();}
      if(action==='attention-open'){const id=target.dataset.id;seen[id]=overviewThreads().find(t=>t.id===id)?.updatedAt||0;writeLocal('dex-attention-seen-v1',seen);close();await window.dexConversationNavigate(id);}
      if(action==='side-open'){const current=side;close();await window.dexConversationNavigate(current.id,current.projectId);}
    }catch(error){notify(error.message);}finally{target.disabled=false;}
  });
  function enhance(container) {
    syncVisibility();
    for(const card of container.querySelectorAll('.message-card.assistant[data-item-id]')) {
      if(card.querySelector('[data-cw-side]'))continue;
      const controls=document.createElement('div');controls.className='cw-message-actions';controls.innerHTML=`<button type="button" data-cw-side="${esc(card.dataset.itemId)}">Perguntar em conversa lateral</button>`;card.append(controls);
    }
  }
  let attentionTimer;
  function refreshOverview() {
    if(attentionTimer)return;
    attentionTimer=setTimeout(()=>{attentionTimer=null;refreshAttentionViews();},350);
  }
  window.addEventListener('dex-conversation-event',event=>{
    const detail=event.detail||{},method=detail.notification?.method||'';
    if(!method||/^(turn\/(started|completed)|thread\/status\/changed)$/.test(method))refreshOverview();
  });
  document.addEventListener('keydown',event=>{
    if((event.ctrlKey||event.metaKey)&&event.shiftKey&&event.key.toLowerCase()==='f'){event.preventDefault();open('search');}
  });
  window.DexConversationWorkspace={enhance,syncVisibility,refreshOverview,diffHTML,commentForm,open,version:'20261017-overview342'};
  refreshOverview();
  enhance(document.getElementById('message-list')||document.createElement('div'));
})();
