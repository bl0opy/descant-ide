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
    // Historical transcripts have no process; a live run for the same repo
    // takes precedence and lends its status.
    const run = S.live.find((r) => r.session_id === sessionData.session_id);
    return run ? run.status : 'idle';
  }

  function renderSidebar() {
    const host = $('sidebar-scroll');
    host.innerHTML = '';

    if (S.live.length) {
      host.appendChild(
        groupEl(
          'Live runs',
          S.live.map((r) => ({
            id: r.run_id,
            type: 'run',
            label: r.prompt || '(no prompt)',
            sub: r.repo_name,
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
        )}</span>`;
      row.addEventListener('click', () =>
        it.type === 'run' ? openRun(it.id) : openSession(it.id)
      );
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
    if (!S.tabs.find((t) => t.key === tab.key)) S.tabs.push(tab);
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
    if (tab.view === 'inspector') {
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
        p.catch((e) => toast(`editor: ${e.message}`, true));
      }
    }
  }

  function closeTab(key) {
    const i = S.tabs.findIndex((t) => t.key === key);
    if (i < 0) return;
    S.tabs.splice(i, 1);
    activateTab(S.tabs.length ? S.tabs[Math.max(0, i - 1)].key : null);
  }

  // ======================================================================
  // agent panel
  // ======================================================================

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

  async function openRun(runId) {
    S.selected = { type: 'run', id: runId };
    renderSidebar();
    closeWs();
    $('agent-log').innerHTML = '';

    const run = S.live.find((r) => r.run_id === runId);
    setAgentHeader({
      title: run?.prompt || 'Live run',
      meta: run?.repo_name || '',
      status: run?.status || 'running',
    });
    renderTargets();
    if (run) {
      $('titlebar-context').textContent = `${run.repo_name} — live`;
      $('panel-cwd').textContent = run.cwd;
      if (S.panelOpen) window.Term.open(run.cwd);
    }

    const url = S.api.replace(/^http/, 'ws') + `/ws/runs/${runId}`;
    const ws = new WebSocket(url);
    S.ws = ws;
    ws.onmessage = (msg) => {
      const ev = JSON.parse(msg.data);
      if (ev.kind === 'ping') return;
      if (ev.kind === 'meta') {
        const i = S.live.findIndex((r) => r.run_id === ev.run.run_id);
        if (i >= 0) S.live[i] = ev.run;
        else S.live.unshift(ev.run);
        setAgentHeader({
          title: ev.run.prompt,
          meta: `${ev.run.repo_name}${
            ev.run.cost_usd ? ' · $' + ev.run.cost_usd.toFixed(3) : ''
          }`,
          status: ev.run.status,
        });
        updateRunControls(ev.run.status);
        renderSidebar();
        return;
      }
      if (ev.kind === 'status' && ev.subtype === 'state') {
        const r = S.live.find((x) => x.run_id === runId);
        if (r) r.status = ev.meta.status;
        $('agent-dot').className = `dot ${ev.meta.status}`;
        updateRunControls(ev.meta.status);
        renderSidebar();
        refreshStatusbar();
      }
      if (ev.kind === 'permission') toast(`${ev.tool_name} needs your decision`, true);
      appendEvent(ev);
    };
    ws.onerror = () => toast('lost the live stream', true);
    ws.onclose = () => {
      if (S.ws === ws) S.ws = null;
    };
  }

  function updateRunControls(status) {
    const active = status === 'running' || status === 'starting';
    $('btn-stop').style.display = active ? '' : 'none';
    $('btn-send').disabled = active;
    $('btn-send').textContent = active ? 'Running…' : 'Run';
  }

  // ======================================================================
  // starting a run
  // ======================================================================

  async function startRun() {
    const prompt = $('composer-input').value.trim();
    if (!prompt) {
      toast('type something for Claude to do first');
      return;
    }
    const cwd = $('composer-target').value || currentRepoPath();
    if (!cwd) {
      toast('no runnable repo — none of these transcripts point at a path on this machine', true);
      return;
    }
    $('btn-send').disabled = true;
    try {
      const run = await api('/api/runs', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ prompt, cwd }),
      });
      $('composer-input').value = '';
      S.live.unshift(run);
      renderSidebar();
      refreshStatusbar();
      await openRun(run.run_id);
    } catch (err) {
      toast(`could not start: ${err.message}`, true);
      $('btn-send').disabled = false;
    }
  }

  /** The repo the selected thing belongs to — whether or not it exists here. */
  function selectedRepo() {
    if (S.selected?.type === 'run') {
      const r = S.live.find((x) => x.run_id === S.selected.id);
      return r ? { repo_path: r.cwd, repo_name: r.repo_name, exists: true } : null;
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
      hint.textContent = `${repo.repo_name} isn't on this machine — running in ${target
        .split('/')
        .pop()}`;
      hint.style.color = 'var(--warning)';
    } else {
      hint.textContent = `runs in ${target || '—'}`;
      hint.style.color = '';
    }
  }

  async function stopRun() {
    if (S.selected?.type !== 'run') return;
    try {
      await api(`/api/runs/${S.selected.id}/stop`, { method: 'POST' });
    } catch (err) {
      toast(err.message, true);
    }
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
    { id: 'terminal', icon: 'terminal', title: 'Terminal', onSelect: togglePanel },
  ];

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
      : 'no live runs';
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

    // Poll for live-run status so the sidebar dots stay honest even when the
    // user is not attached to that run's socket.
    setInterval(async () => {
      try {
        const { runs } = await api('/api/runs');
        S.live = runs;
        if (S.activeView === 'sessions') renderSidebar();
        renderActivityBar();
        refreshStatusbar();
      } catch {
        /* backend hiccup; the next tick will tell the truth */
      }
    }, 2500);

    $('btn-refresh').addEventListener('click', refresh);
    $('btn-send').addEventListener('click', startRun);
    $('btn-stop').addEventListener('click', stopRun);
    $('btn-panel-close').addEventListener('click', () => togglePanel(false));
    $('composer-target').addEventListener('change', updateComposerHint);

    $('composer-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        startRun();
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

  window.addEventListener('DOMContentLoaded', () =>
    boot().catch((err) => {
      document.body.innerHTML = `<pre style="padding:24px;color:#f87171">Descant failed to boot:\n${
        err.stack || err
      }</pre>`;
    })
  );
})();
