// Descant renderer — application controller.
//
// Owns state and wiring only.  Rendering lives in views.js, the terminal in
// term.js, the editor in editor.js.  Data comes from the Python backend over
// HTTP + WebSocket; nothing is computed here that the backend already knows.

(() => {
  const { esc, fmtTokens, ago, renderEvent, renderLog, renderInspector, toolSummary } =
    window.Views;

  const S = {
    api: 'http://127.0.0.1:8787',
    config: null,
    repos: [],
    live: [],           // live runs, newest first
    selected: null,     // {type:'session'|'run', id}
    tabs: [],           // {key,label,view,payload}
    activeTab: null,
    ws: null,
    logEvents: [],
    collapsed: new Set(),
    activeView: 'sessions',
    panelOpen: false,
    fileCache: new Map(),
    panelMode: 'chat',  // 'chat' = live conversation, 'session' = read-only transcript
    chatCwd: null,      // which repo's chat the panel is showing
  };

  const $ = (id) => document.getElementById(id);

  // ======================================================================
  // helpers
  // ======================================================================

  let toastTimer = null;
  function toast(msg, isError = false) {
    const el = $('toast');
    el.textContent = msg;
    el.className = 'show' + (isError ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.className = ''), 4200);
  }

  const post = (path, body) =>
    api(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });

  async function api(path, opts) {
    const res = await fetch(S.api + path, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch {
        /* not json */
      }
      throw new Error(detail);
    }
    return res.json();
  }

  // ======================================================================
  // sidebar
  // ======================================================================

  function statusOfSession(sessionData) {
    // History is idle by definition; a session we are actively following lends
    // its live status to the matching row.
    const live = S.live.find((r) => r.sessionId === sessionData.session_id);
    return live ? live.status : 'idle';
  }

  function renderSidebar() {
    const host = $('sidebar-scroll');
    host.innerHTML = '';

    if (S.live.length) {
      host.appendChild(
        groupEl(
          'Live runs',
          S.live.map((r) => ({
            id: r.sessionId || r.cwd,
            type: 'live',
            label: r.prompt || r.sessionId || '(starting…)',
            sub: r.cwd.split('/').pop(),
            status: r.status,
          })),
          'live'
        )
      );
    }

    for (const repo of S.repos) {
      host.appendChild(
        groupEl(
          repo.repo_name,
          repo.sessions.map((s) => ({
            id: s.session_id,
            type: 'session',
            label: s.title,
            sub: ago(s.last_activity),
            status: statusOfSession(s),
            tokens: s.context.total_tokens,
          })),
          repo.repo_path,
          repo.repo_path
        )
      );
    }

    if (!S.repos.length && !S.live.length) {
      const p = document.createElement('div');
      p.className = 'empty-state';
      p.innerHTML = `<p>No transcripts found in<br><code>${esc(
        S.config?.projectsDir || ''
      )}</code></p>`;
      host.appendChild(p);
    }
  }

  function groupEl(name, items, key, titleAttr) {
    const wrap = document.createElement('div');
    wrap.className = 'repo-group' + (S.collapsed.has(key) ? ' collapsed' : '');

    const head = document.createElement('div');
    head.className = 'repo-header';
    head.title = titleAttr || name;
    head.innerHTML = `<span class="chevron">${window.Icons.chevronDown}</span>
      <span class="repo-name">${esc(name)}</span>
      <span class="count">${items.length}</span>`;
    head.addEventListener('click', () => {
      if (S.collapsed.has(key)) S.collapsed.delete(key);
      else S.collapsed.add(key);
      wrap.classList.toggle('collapsed');
    });
    wrap.appendChild(head);

    const list = document.createElement('div');
    list.className = 'repo-sessions';
    for (const it of items) {
      const row = document.createElement('div');
      const isActive = S.selected?.type === it.type && S.selected?.id === it.id;
      row.className = 'session-row' + (isActive ? ' active' : '');
      row.title = it.label;
      row.innerHTML = `<i class="dot ${esc(it.status)}"></i>
        <span class="label">${esc(it.label)}</span>
        <span class="sub">${esc(
          it.tokens != null ? fmtTokens(it.tokens) : it.sub || ''
        )}</span>
        ${
          it.type === 'session'
            ? `<button class="row-delete" title="Delete this session's transcript">${window.Icons.close}</button>`
            : ''
        }`;
      row.addEventListener('click', (e) => {
        if (e.target.closest('.row-delete')) {
          e.stopPropagation();
          deleteSession(it.id, it.label);
          return;
        }
        return it.type === 'live'
          ? followRepo(
              S.live.find((r) => (r.sessionId || r.cwd) === it.id).cwd,
              S.live.find((r) => (r.sessionId || r.cwd) === it.id).sessionId
            )
          : openSession(it.id);
      });
      list.appendChild(row);
    }
    wrap.appendChild(list);
    return wrap;
  }

  // ======================================================================
  // tabs
  // ======================================================================

  function renderTabs() {
    const host = $('tabs');
    host.innerHTML = '';
    for (const t of S.tabs) {
      const el = document.createElement('div');
      el.className = 'tab' + (t.key === S.activeTab ? ' active' : '');
      el.innerHTML = `<span>${esc(t.label)}</span><span class="close">${window.Icons.close}</span>`;
      el.addEventListener('click', (e) => {
        if (e.target.closest('.close')) {
          closeTab(t.key);
          return;
        }
        activateTab(t.key);
      });
      host.appendChild(el);
    }
  }

  function openTab(tab) {
    const existing = S.tabs.find((t) => t.key === tab.key);
    if (existing) {
      // Re-opening replaces the payload/renderer: a loadout tab reopened after
      // a toggle must draw the new state, not the closure it was created with.
      Object.assign(existing, tab);
    } else {
      S.tabs.push(tab);
    }
    activateTab(tab.key);
  }

  function activateTab(key) {
    S.activeTab = key;
    const tab = S.tabs.find((t) => t.key === key);
    renderTabs();
    for (const v of document.querySelectorAll('.view')) v.classList.remove('active');
    if (!tab) {
      $('view-welcome').classList.add('active');
      return;
    }
    if (tab.view === 'panel') {
      showLanguage(null);
      $('view-panel-generic').classList.add('active');
      tab.render($('view-panel-generic'));
      return;
    }
    if (tab.view === 'inspector') {
      showLanguage(null);
      $('view-inspector').classList.add('active');
      renderInspector($('view-inspector'), tab.payload);
    } else if (tab.view === 'editor' || tab.view === 'diff') {
      $('view-editor').classList.add('active');
      window.Editor.layout();
      // Monaco hosts one editor at a time, so re-open when the tab changes what
      // should be showing.
      const wanted = tab.view === 'diff' ? `diff:${tab.payload.path}` : tab.payload.path;
      if (window.Editor.current() !== wanted) {
        const p =
          tab.view === 'diff'
            ? window.Editor.openDiff(tab.payload.path, tab.payload.before, tab.payload.after)
            : window.Editor.openFile(tab.payload.path, tab.payload.text);
        p.then(() => showLanguage(tab.payload.path)).catch((e) =>
          toast(`editor: ${e.message}`, true)
        );
      }
    }
  }

  function closeTab(key) {
    const i = S.tabs.findIndex((t) => t.key === key);
    if (i < 0) return;
    if (key.startsWith('chat:')) disposeChat(key.slice(5));
    S.tabs.splice(i, 1);
    activateTab(S.tabs.length ? S.tabs[Math.max(0, i - 1)].key : null);
  }

  // ======================================================================
  // agent panel
  // ======================================================================

  /** Status-bar language indicator, straight from Monaco's own detection. */
  function showLanguage(path) {
    const el = $('status-lang');
    if (!path) {
      el.textContent = '';
      return;
    }
    const id = window.Editor.langFor(path);
    el.textContent = id === 'plaintext' ? 'Plain Text' : id;
    el.title = `${window.Editor.languageCount()} languages available`;
  }

  function setAgentHeader({ title, meta, status }) {
    $('agent-title').textContent = title || 'Agent';
    $('agent-meta').textContent = meta || '';
    $('agent-dot').className = `dot ${status || 'idle'}`;
  }

  function appendEvent(ev) {
    const host = $('agent-log');
    const nearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 120;
    const el = renderEvent(ev, { container: host, onDiff: openDiff });
    if (el) host.appendChild(el);
    if (nearBottom) host.scrollTop = host.scrollHeight;
  }

  function closeWs() {
    if (S.ws) {
      S.ws.onclose = null;
      S.ws.close();
      S.ws = null;
    }
  }

  // ======================================================================
  // opening things
  // ======================================================================

  async function openSession(sessionId) {
    S.selected = { type: 'session', id: sessionId };
    S.panelMode = 'session';
    renderSidebar();
    closeWs();
    try {
      const data = await api(`/api/sessions/${sessionId}`);
      S.logEvents = data.events;
      setAgentHeader({
        title: data.title,
        meta: `${data.message_count} msgs · ${fmtTokens(data.context.total_tokens)}`,
        status: 'idle',
      });
      renderLog($('agent-log'), data.events, { onDiff: openDiff });
      // The panel is showing history now, not a conversation — say so, and
      // leave an obvious way back to the live one.
      $('btn-back-to-chat').style.display = S.chatCwd ? '' : 'none';
      $('btn-send').disabled = false;
      $('btn-stop').style.display = 'none';
      renderTargets();
      $('titlebar-context').textContent = `${data.repo_name} — ${data.git_branch || 'no branch'}`;
      $('status-context').textContent = `context ${fmtTokens(
        data.context.total_tokens
      )} · ${data.measured.available ? '$' + data.measured.usd.toFixed(2) : 'no usage data'}`;
      openTab({
        key: `ctx:${sessionId}`,
        label: `${data.repo_name} · context`,
        view: 'inspector',
        payload: data,
      });
      if (S.panelOpen) window.Term.open(data.repo_path);
      $('panel-cwd').textContent = data.repo_path;
    } catch (err) {
      toast(`could not open session: ${err.message}`, true);
    }
  }

  function updateRunControls(status) {
    $('btn-stop').style.display =
      status === 'running' || status === 'needs_input' ? '' : 'none';
  }

  // ======================================================================
  // following a live session by its transcript
  // ======================================================================

  function upsertLive(entry) {
    const key = entry.sessionId || entry.cwd;
    const i = S.live.findIndex((r) => (r.sessionId || r.cwd) === key);
    if (i >= 0) S.live[i] = { ...S.live[i], ...entry };
    else S.live.unshift(entry);
    renderSidebar();
    renderActivityBar();
    refreshStatusbar();
  }

  function followRepo(cwd, sessionId = null, prompt = null) {
    S.selected = { type: 'live', cwd, sessionId };
    upsertLive({ cwd, sessionId, status: 'running', prompt });
    closeWs();
    $('agent-log').innerHTML = '';
    setAgentHeader({
      title: sessionId ? `continuing ${sessionId.slice(0, 8)}` : 'live session',
      meta: cwd.split('/').pop(),
      status: 'running',
    });
    $('titlebar-context').textContent = `${cwd.split('/').pop()} — live`;

    const qs = new URLSearchParams({ cwd, from_start: 'true' });
    if (sessionId) qs.set('session_id', sessionId);
    const ws = new WebSocket(`${S.api.replace(/^http/, 'ws')}/ws/tail?${qs}`);
    S.ws = ws;
    S.liveSessionId = sessionId;

    ws.onmessage = (msg) => {
      const ev = JSON.parse(msg.data);
      if (ev.kind === 'ping') return;
      if (ev.kind === 'meta') return;
      if (ev.kind === 'status' && ev.subtype === 'attached') {
        const prev = S.liveSessionId;
        S.liveSessionId = ev.meta?.session_id || S.liveSessionId;
        S.selected = { type: 'live', cwd, sessionId: S.liveSessionId };
        // The placeholder row was keyed on cwd before we knew the id.
        if (!prev) S.live = S.live.filter((r) => r.sessionId || r.cwd !== cwd);
        upsertLive({ cwd, sessionId: S.liveSessionId, status: 'running' });
        setAgentHeader({
          title: `live · ${(S.liveSessionId || '').slice(0, 8)}`,
          meta: cwd.split('/').pop(),
          status: 'running',
        });
      }
      if (ev.kind === 'status' && ev.subtype === 'state') {
        const st = ev.meta?.status || 'idle';
        $('agent-dot').className = `dot ${st}`;
        updateRunControls(st);
        upsertLive({ cwd, sessionId: S.liveSessionId, status: st });
        if (st === 'needs_input') toast('Claude is waiting on you in the terminal', true);
        return;
      }
      appendEvent(ev);
    };
    ws.onclose = () => {
      if (S.ws === ws) S.ws = null;
    };
  }

  /** The repo the selected thing belongs to — whether or not it exists here. */
  function selectedRepo() {
    if (S.selected?.type === 'live') {
      return {
        repo_path: S.selected.cwd,
        repo_name: S.selected.cwd.split('/').pop(),
        exists: true,
      };
    }
    if (S.selected?.type === 'session') {
      return (
        S.repos.find((repo) =>
          repo.sessions.some((s) => s.session_id === S.selected.id)
        ) || null
      );
    }
    return null;
  }

  /** Where file/terminal views point: only ever a path that exists. */
  function currentRepoPath() {
    const repo = selectedRepo();
    if (repo && repo.exists !== false) return repo.repo_path;
    return S.config?.sandboxRepo;
  }

  /** Populate the run-target picker with repos that actually exist here. */
  function renderTargets() {
    const sel = $('composer-target');
    const previous = sel.value;
    const opts = S.repos
      .filter((r) => r.exists)
      .map((r) => ({ value: r.repo_path, label: r.repo_name }));
    if (S.config?.sandboxRepo && !opts.some((o) => o.value === S.config.sandboxRepo)) {
      opts.push({ value: S.config.sandboxRepo, label: 'sandbox-repo (scratch)' });
    }
    sel.innerHTML = opts
      .map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`)
      .join('');

    const repo = selectedRepo();
    if (repo?.exists && opts.some((o) => o.value === repo.repo_path)) {
      sel.value = repo.repo_path;
    } else if (previous && opts.some((o) => o.value === previous)) {
      sel.value = previous;
    }
    updateComposerHint();
  }

  function updateComposerHint() {
    const repo = selectedRepo();
    const target = $('composer-target').value;
    const hint = $('composer-hint');
    if (repo && repo.exists === false) {
      hint.textContent = `${repo.repo_name} isn't on this machine — using ${target
        .split('/')
        .pop()}`;
      hint.style.color = 'var(--warning)';
    } else {
      hint.textContent = `Send chats · Run shells · ${(target || '—').split('/').pop()}`;
      hint.style.color = '';
    }
  }

  /** Ctrl-C into the repo's shell — the session lives there now. */
  function stopRun() {
    const cwd = S.selected?.cwd || currentRepoPath();
    if (!cwd || !window.Term.send(cwd, '\u0003')) {
      toast('no terminal running for this repo');
      return;
    }
    togglePanel(true);
  }

  // ======================================================================
  // file explorer view
  // ======================================================================

  async function showFiles() {
    const cwd = currentRepoPath();
    const host = $('sidebar-scroll');
    $('sidebar-title').textContent = 'Explorer';
    if (!cwd) {
      host.innerHTML = `<div class="empty-state"><p>Open a session first.</p></div>`;
      return;
    }
    host.innerHTML = `<div class="empty-state"><p>Loading ${esc(cwd)}…</p></div>`;
    try {
      const data = await api(`/api/files?path=${encodeURIComponent(cwd)}`);
      host.innerHTML = '';
      if (!data.files.length) {
        host.innerHTML = `<div class="empty-state"><p>No files under<br><code>${esc(
          cwd
        )}</code></p><p>The repo this transcript refers to does not exist on this machine.</p></div>`;
        return;
      }
      for (const f of data.files) {
        const row = document.createElement('div');
        row.className = 'session-row';
        row.title = f.rel;
        row.innerHTML = `<span class="label">${esc(f.rel)}</span>
          <span class="sub">${(f.size / 1024).toFixed(0)}k</span>`;
        row.addEventListener('click', () => openFile(f.path, f.rel));
        host.appendChild(row);
      }
    } catch (err) {
      host.innerHTML = `<div class="empty-state"><p>${esc(err.message)}</p></div>`;
    }
  }

  /**
   * Show what an agent edit changed.
   *
   * The transcript records the edit, not the file's before/after state, so we
   * reconstruct: read the file as it stands now and apply the edit backwards.
   * If the file is gone (a transcript from another machine), fall back to
   * diffing the edit's own old_string against new_string, which still shows the
   * change itself, just without surrounding context.
   */
  async function openDiff(ev) {
    const input = ev.tool_input || {};
    const path = input.file_path || input.path;
    if (!path) {
      toast('that edit has no file path to diff');
      return;
    }
    const label = path.split('/').pop();
    let after = null;
    try {
      const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
      if (!data.binary) after = data.text;
    } catch {
      /* file not on this machine — handled below */
    }

    let before;
    if (ev.tool_name === 'Write') {
      before = after !== null && after !== input.content ? after : '';
      after = input.content ?? '';
    } else if (after !== null && input.old_string && after.includes(input.new_string ?? '')) {
      // Un-apply the edit to reconstruct the previous file contents.
      before = after.replace(input.new_string, input.old_string);
    } else {
      before = input.old_string ?? '';
      after = input.new_string ?? after ?? '';
      toast(`${label} isn't on this machine — showing the edit without context`);
    }

    openTab({
      key: `diff:${ev.tool_use_id || path}`,
      label: `${label} (diff)`,
      view: 'diff',
      payload: { path, before, after },
    });
    try {
      await window.Editor.openDiff(path, before, after);
    } catch (err) {
      toast(`diff failed: ${err.message}`, true);
    }
  }

  async function openFile(path, label) {
    try {
      const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
      if (data.binary) {
        toast('binary file — not opening');
        return;
      }
      openTab({
        key: `file:${path}`,
        label: label || path.split('/').pop(),
        view: 'editor',
        payload: { path, text: data.text },
      });
      await window.Editor.openFile(path, data.text);
    } catch (err) {
      toast(`could not open file: ${err.message}`, true);
    }
  }

  // ======================================================================
  // activity bar
  // ======================================================================

  const ACTIVITIES = [
    { id: 'sessions', icon: 'sessions', title: 'Sessions', onSelect: showSessions },
    { id: 'files', icon: 'files', title: 'Explorer', onSelect: showFiles },
    { id: 'loadout', icon: 'loadout', title: 'Tool loadout', onSelect: showLoadout },
    { id: 'mining', icon: 'mining', title: 'Mined workflows', onSelect: showMining },
    { id: 'library', icon: 'library', title: 'Capability library', onSelect: showLibrary },
    { id: 'terminal', icon: 'terminal', title: 'Terminal', onSelect: togglePanel },
  ];

  // ======================================================================
  // Loadout / mining / library  (features 2-5)
  // ======================================================================

  /** Open (or re-render) a panel tab backed by a fetch + a renderer. */
  function openPanelTab(key, label, render) {
    openTab({ key, label, view: 'panel', render });
  }

  function loadoutRepo() {
    return currentRepoPath() || $('composer-target').value;
  }

  async function showLoadout() {
    const repo = loadoutRepo();
    if (!repo) {
      toast('no repo on this machine to inspect', true);
      return;
    }
    openPanelTab('loadout:' + repo, `Loadout · ${repo.split('/').pop()}`, async (host) => {
      host.innerHTML = '<div class="inspector"><p class="muted">Probing MCP servers…</p></div>';
      try {
        const data = await api(`/api/loadout?repo=${encodeURIComponent(repo)}`);
        window.Panels.renderLoadout(host, data, {
          toggleMcp: async (name, enabled) => {
            await api('/api/loadout/mcp', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ repo, name, enabled }),
            });
            toast(`${name} ${enabled ? 'attached' : 'detached'}`);
            showLoadout();
          },
          toggleSkill: async (name, enabled) => {
            await api('/api/loadout/skill', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ repo, name, enabled }),
            });
            showLoadout();
          },
          testMcp: (name, config) => post('/api/loadout/mcp/test', { repo, name, config }),
          saveMcp: async (name, config, scope) => {
            const res = await post('/api/loadout/mcp/save', { repo, name, config, scope });
            toast(`saved ${res.name} · ${res.written}`);
            showLoadout();
          },
          deleteMcp: async (name, source) => {
            const where = source === '.mcp.json' ? 'the checked-in .mcp.json' : 'your local config';
            if (!confirm(`Remove ${name} from ${where}?`)) return;
            await post('/api/loadout/mcp/delete', { repo, name });
            toast(`removed ${name}`);
            showLoadout();
          },
          previewConversion: async (name) => {
            host.innerHTML =
              '<div class="inspector"><p class="muted">Generating skill…</p></div>';
            const preview = await api('/api/mcp/preview', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ repo, name }),
            });
            window.Panels.renderConversionPreview(host, preview, {
              cancel: showLoadout,
              confirmConversion: async (serverName) => {
                const res = await api('/api/mcp/convert', {
                  method: 'POST',
                  headers: { 'content-type': 'application/json' },
                  body: JSON.stringify({ repo, name: serverName, disable_after: true }),
                });
                toast(
                  `wrote ${res.slug} skill · saved ~${res.saved_tokens} tokens per turn`
                );
                showLoadout();
              },
            });
          },
        });
      } catch (err) {
        host.innerHTML = `<div class="inspector"><p class="muted">${err.message}</p></div>`;
      }
    });
  }

  async function showMining() {
    const repo = loadoutRepo();
    const qs = repo ? `?repo=${encodeURIComponent(repo)}&limit=20` : '?limit=20';
    openPanelTab('mining', 'Mined workflows', async (host) => {
      host.innerHTML = '<div class="inspector"><p class="muted">Diffing transcripts…</p></div>';
      try {
        let data = await api('/api/mining' + qs);
        // A single repo often has too little history; fall back to everything
        // rather than showing an empty panel that looks broken.
        if (!data.candidates.length && repo) data = await api('/api/mining?limit=20');
        window.Panels.renderMining(host, data, {
          acceptMined: async (c, opts = {}) => {
            const scope = opts.scope || 'repo';
            const target = repo || c.repos[0];
            if (scope === 'repo' && !target) {
              throw new Error('no repo on this machine to write the skill into');
            }
            const payload = {
              repo: target || '',
              slug: opts.slug || c.proposed_skill.slug,
              skill_md: c.proposed_skill.skill_md,
              scope,
            };
            let res;
            try {
              res = await post('/api/mining/accept', payload);
            } catch (err) {
              // A name collision is a question, not a failure: the usual answer
              // is "yes, replace the older version of the same workflow".
              if (!/already exists/.test(err.message)) throw err;
              if (!confirm(`${payload.slug} already exists. Replace it?`)) return;
              res = await post('/api/mining/accept', { ...payload, overwrite: true });
            }
            toast(`created ${res.slug} — available to ${res.available_to}`);
            return res;
          },
        });
      } catch (err) {
        host.innerHTML = `<div class="inspector"><p class="muted">${err.message}</p></div>`;
      }
    });
  }

  /**
   * Delete a session's transcript.
   *
   * The transcript is the only record of the conversation, so this asks first
   * and says what it is about to destroy.
   */
  async function deleteSession(sessionId, label) {
    const what = label && label.length > 60 ? label.slice(0, 60) + '…' : label;
    if (!confirm(`Delete this session permanently?\n\n${what}\n\nIts transcript is the only record — this cannot be undone.`)) {
      return;
    }
    try {
      const res = await api(`/api/sessions/${sessionId}`, { method: 'DELETE' });
      toast(`deleted ${res.title ? '“' + res.title.slice(0, 40) + '”' : sessionId.slice(0, 8)}`);
      if (S.selected?.type === 'session' && S.selected.id === sessionId) {
        S.selected = null;
        closeTab(`ctx:${sessionId}`);
        if (S.chatCwd) showChat(S.chatCwd);
      }
      await refresh();
    } catch (err) {
      toast(`could not delete: ${err.message}`, true);
    }
  }

  /** Start a fresh conversation, and put the panel on it. */
  async function newSession() {
    const cwd = $('composer-target').value || currentRepoPath();
    if (!cwd) {
      toast('no repo on this machine to start a session in', true);
      return;
    }
    // A repo's chat is its conversation; dropping the old one starts a new one.
    const old = CHATS.get(cwd);
    if (old) {
      old.dispose();
      CHATS.delete(cwd);
      post(`/api/chats/${old.meta.chat_id}/close`, {}).catch(() => {});
    }
    await showChat(cwd);
    toast(`new session in ${cwd.split('/').pop()}`);
  }

  // ======================================================================
  // Chat — the agent panel *is* the conversation
  // ======================================================================
  //
  // The panel has two jobs and one set of elements: it shows a live chat, and
  // it shows a read-only transcript when you open a past session. `S.panelMode`
  // says which, so the composer knows whether Send means "talk to Claude" and
  // the log knows whether it is safe to overwrite.

  const CHATS = new Map(); // cwd -> handle

  function activeChat() {
    return S.chatCwd ? CHATS.get(S.chatCwd) : null;
  }

  /** The chat for a repo, started if this is the first time we need it. */
  async function chatFor(cwd) {
    const existing = CHATS.get(cwd);
    if (existing) return existing;
    const chat = await post('/api/chats', { repo: cwd, permission_mode: $('chat-mode').value });
    const handle = window.Chat.attach({
      chat,
      els: { log: $('agent-log') },
      api,
      wsUrl: (id) => `${S.api.replace(/^http/, 'ws')}/ws/chats/${id}`,
      onDiff: openDiff,
      onMeta: (m) => {
        // Only the chat currently on screen may drive the panel's chrome.
        if (S.panelMode === 'chat' && S.chatCwd === m.cwd) paintChatHeader(m);
      },
    });
    CHATS.set(cwd, handle);
    return handle;
  }

  function paintChatHeader(m) {
    setAgentHeader({
      title: m.repo_name || m.cwd.split('/').pop(),
      meta: window.Chat.STATUS_LABEL[m.status] || m.status,
      status: m.status === 'running' ? 'running' : m.status === 'needs_input' ? 'needs_input' : m.status === 'failed' ? 'failed' : 'idle',
    });
    $('chat-mode').value = m.permission_mode;
    const busy = m.status === 'running' || m.status === 'starting';
    $('btn-send').disabled = busy;
    $('btn-stop').style.display = busy ? '' : 'none';
  }

  /** Bring the live conversation for *cwd* back onto the panel. */
  async function showChat(cwd) {
    cwd = cwd || $('composer-target').value || currentRepoPath();
    if (!cwd) {
      toast('no repo on this machine to chat about — pick one in the dropdown', true);
      return;
    }
    let handle;
    try {
      handle = await chatFor(cwd);
    } catch (err) {
      toast(`could not start a chat: ${err.message}`, true);
      return;
    }
    S.panelMode = 'chat';
    S.chatCwd = cwd;
    closeWs(); // stop tailing a transcript into the same log
    if ($('composer-target').value !== cwd) $('composer-target').value = cwd;
    handle.repaint();
    paintChatHeader(handle.meta);
    updateComposerHint();
    $('btn-back-to-chat').style.display = 'none';
    $('composer-input').focus();
  }

  /** Send the composer's contents as a chat turn. */
  async function sendChat() {
    const text = $('composer-input').value.trim();
    if (!text) {
      toast('type something for Claude to do first');
      return;
    }
    const cwd = $('composer-target').value || currentRepoPath();
    if (!cwd) {
      toast('no runnable repo — none of these transcripts point at a path on this machine', true);
      return;
    }
    if (S.panelMode !== 'chat' || S.chatCwd !== cwd) await showChat(cwd);
    const handle = activeChat();
    if (!handle) return;
    $('composer-input').value = '';
    try {
      await handle.send(text);
    } catch {
      $('composer-input').value = text; // give it back rather than losing it
    }
  }

  /**
   * Run the composer's contents in the terminal, as a shell command.
   *
   * Deliberately literal: what you typed is what bash gets. Send talks to
   * Claude; Run runs a command. Keeping those two obviously different is the
   * whole point of having both.
   */
  async function runInTerminal() {
    const command = $('composer-input').value.trim();
    if (!command) {
      toast('type a command to run first');
      return;
    }
    const cwd = $('composer-target').value || currentRepoPath();
    if (!cwd) {
      toast('no repo on this machine to run in', true);
      return;
    }
    if (!window.Term.available()) {
      toast('terminal unavailable — see the console', true);
      return;
    }
    togglePanel(true);
    const opened = await window.Term.open(cwd);
    if (opened && opened.ok === false) {
      toast(opened.error, true);
      return;
    }
    // A freshly spawned pty has not drawn its prompt yet; typing immediately
    // races the shell and the line can be swallowed.
    setTimeout(
      () => {
        if (!window.Term.send(cwd, command)) toast('terminal went away before launch', true);
      },
      opened && opened.reused ? 0 : 400
    );
    $('composer-input').value = '';
  }

  async function stopChat() {
    const handle = activeChat();
    if (S.panelMode === 'chat' && handle) {
      await handle.interrupt();
      return;
    }
    stopRun(); // a terminal session is stopped with Ctrl-C, as before
  }

  async function showLibrary(query = '') {
    openPanelTab('library', 'Capability library', async (host) => {
      host.innerHTML = '<div class="inspector"><p class="muted">Indexing…</p></div>';
      try {
        const data = await api(`/api/library?q=${encodeURIComponent(query)}&limit=25`);
        window.Panels.renderLibrary(host, data, {
          copySkill: async (entry, target) => {
            const body =
              target === 'user'
                ? { source_path: entry.path, scope: 'user' }
                : { source_path: entry.path, scope: 'repo', repo: target };
            try {
              return await post('/api/skills/copy', body);
            } catch (err) {
              if (!/already exists/.test(err.message)) throw err;
              if (!confirm(`${entry.name} already exists there. Replace it?`)) throw err;
              return post('/api/skills/copy', { ...body, overwrite: true });
            }
          },
          search: (q) => {
            query = q;
            const tab = S.tabs.find((t) => t.key === 'library');
            if (tab) tab.render(host);
          },
        });
      } catch (err) {
        host.innerHTML = `<div class="inspector"><p class="muted">${err.message}</p></div>`;
      }
    });
  }

  function showSessions() {
    $('sidebar-title').textContent = 'Sessions';
    renderSidebar();
  }

  function renderActivityBar() {
    const host = $('activity-bar');
    host.innerHTML = '';
    for (const a of ACTIVITIES) {
      const btn = document.createElement('button');
      btn.className = 'activity-btn' + (S.activeView === a.id ? ' active' : '');
      btn.title = a.title;
      btn.innerHTML = window.Icons[a.icon];
      if (a.id === 'sessions' && S.live.some((r) => r.status === 'needs_input')) {
        const b = document.createElement('span');
        b.className = 'badge';
        b.textContent = String(S.live.filter((r) => r.status === 'needs_input').length);
        btn.appendChild(b);
      }
      btn.addEventListener('click', () => {
        if (a.id !== 'terminal') S.activeView = a.id;
        renderActivityBar();
        a.onSelect();
      });
      host.appendChild(btn);
    }
  }

  function togglePanel(force) {
    S.panelOpen = force === undefined ? !S.panelOpen : force;
    $('panel').classList.toggle('hidden', !S.panelOpen);
    if (S.panelOpen) {
      const cwd = currentRepoPath();
      $('panel-cwd').textContent = cwd || '';
      window.Term.open(cwd).then((res) => {
        if (res && !res.ok) toast(res.error, true);
      });
      setTimeout(() => window.Term.fitActive(), 30);
    }
    window.Editor.layout();
  }

  // ======================================================================
  // status bar + polling
  // ======================================================================

  function refreshStatusbar() {
    const sessions = S.repos.reduce((n, r) => n + r.sessions.length, 0);
    $('status-sessions').textContent = `${sessions} session${
      sessions === 1 ? '' : 's'
    } · ${S.repos.length} repo${S.repos.length === 1 ? '' : 's'}`;
    const running = S.live.filter((r) => r.status === 'running').length;
    const waiting = S.live.filter((r) => r.status === 'needs_input').length;
    const el = $('status-runs');
    el.className = 'item' + (waiting ? ' warn' : '');
    el.textContent = S.live.length
      ? `${running} running · ${waiting} need you`
      : 'no live sessions';
  }

  async function refresh() {
    try {
      const data = await api('/api/sessions');
      S.repos = data.repos;
      S.live = data.live;
      $('status-backend').textContent = 'backend up';
      $('status-backend').className = 'item';
      $('titlebar-source').textContent = data.projects_dir;
      if (S.activeView === 'sessions') renderSidebar();
      renderActivityBar();
      renderTargets();
      refreshStatusbar();
    } catch (err) {
      $('status-backend').textContent = `backend down — ${err.message}`;
      $('status-backend').className = 'item err';
    }
  }

  // ======================================================================
  // boot
  // ======================================================================

  async function boot() {
    S.config = await window.descant.config();
    S.api = S.config.api;

    window.Term.init($('terminal-host'));
    window.Editor.init($('monaco-host'));

    renderActivityBar();
    await refresh();
    // The panel is a conversation by default; opening a session switches it to
    // that session's transcript, and "Back to chat" returns.
    showChat().catch(() => {
      /* no repo on this machine yet — the composer says so when you type */
    });

    $('btn-refresh').addEventListener('click', refresh);
    $('btn-new-session').addEventListener('click', newSession);
    $('btn-send').addEventListener('click', sendChat);
    $('btn-run-terminal').addEventListener('click', runInTerminal);
    $('btn-back-to-chat').addEventListener('click', () => showChat(S.chatCwd));
    $('chat-mode').addEventListener('change', async () => {
      const handle = activeChat();
      if (handle) await handle.setMode($('chat-mode').value);
    });
    $('btn-stop').addEventListener('click', stopChat);
    $('btn-panel-close').addEventListener('click', () => togglePanel(false));
    $('composer-target').addEventListener('change', () => {
      updateComposerHint();
      if (S.panelMode === 'chat') showChat($('composer-target').value);
    });

    $('composer-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        sendChat();
      }
    });

    window.addEventListener('keydown', (e) => {
      if (e.key === '`' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        togglePanel();
      }
      if (e.key === 'w' && (e.ctrlKey || e.metaKey) && S.activeTab) {
        e.preventDefault();
        closeTab(S.activeTab);
      }
    });

    window.descant.onBackendExternal((url) => {
      $('status-backend').textContent = `backend up (external ${url})`;
      $('status-backend').className = 'item';
    });

    window.descant.onBackendDown((log) => {
      $('status-backend').textContent = 'backend exited';
      $('status-backend').className = 'item err';
      toast(`Backend exited. Last lines:\n${log.slice(-3).join('\n')}`, true);
    });

    if (!window.Term.available()) {
      console.warn('terminal unavailable:', window.Term.loadError());
    }
  }

  // Exposed for headless verification (DESCANT_DRIVE); harmless in normal use.
  window.followRepoForTest = followRepo;

  window.addEventListener('DOMContentLoaded', () =>
    boot().catch((err) => {
      document.body.innerHTML = `<pre style="padding:24px;color:#f87171">Descant failed to boot:\n${
        err.stack || err
      }</pre>`;
    })
  );
})();
