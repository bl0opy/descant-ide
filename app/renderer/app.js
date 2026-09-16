// Descant renderer — application controller.
//
// Owns state and wiring only. Rendering lives in views.js, the terminal in
// term.js, the editor in editor.js. Data comes from the Python backend over
// HTTP + WebSocket; nothing is computed here that the backend already knows.
//
// The unit of work is a *folder you opened*, not a session someone recorded.
// Everything below — the explorer, search, source control, the terminal's cwd,
// the chat's repo — hangs off `S.folder`.

(() => {
  const { esc, renderEvent } = window.Views;

  const S = {
    api: 'http://127.0.0.1:8787',
    config: null,
    folder: null,       // absolute path of the open folder
    recents: [],        // recently opened folders, most recent first
    git: null,          // last /api/git/status payload
    tabs: [],           // {key,label,view,payload}
    activeTab: null,
    activeView: 'files',
    panelOpen: false,
    chatCwd: null,      // which folder's chat the panel is showing
    chatOptions: null,  // modes + models, fetched once from the backend
    openFilePath: null, // file showing in the editor, for the Run arrow
    truncatedFiles: new Set(), // loaded partially — never safe to save back
  };

  const RECENTS_KEY = 'descant.recentFolders';
  const LAST_KEY = 'descant.lastFolder';
  const MOD = navigator.platform.includes('Mac') ? '⌘' : 'Ctrl';

  const $ = (id) => document.getElementById(id);
  const el = (html) => {
    const d = document.createElement('div');
    d.innerHTML = html.trim();
    return d.firstElementChild;
  };
  const base = (p) => (p || '').split('/').filter(Boolean).pop() || p || '';

  // ======================================================================
  // helpers
  // ======================================================================

  let toastTimer = null;
  function toast(msg, isError = false) {
    const node = $('toast');
    node.textContent = msg;
    node.className = 'show' + (isError ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (node.className = ''), 4200);
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

  const post = (path, body) =>
    api(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });

  function fmtBytes(n) {
    if (n == null) return '';
    if (n < 1024) return `${n}b`;
    if (n < 1024 * 1024) return `${Math.round(n / 1024)}k`;
    return `${(n / 1048576).toFixed(1)}M`;
  }

  // ======================================================================
  // the open folder
  // ======================================================================

  function loadRecents() {
    try {
      S.recents = JSON.parse(localStorage.getItem(RECENTS_KEY) || '[]').filter(Boolean);
    } catch {
      S.recents = [];
    }
    return S.recents;
  }

  function rememberFolder(path) {
    S.recents = [path, ...S.recents.filter((p) => p !== path)].slice(0, 10);
    try {
      localStorage.setItem(RECENTS_KEY, JSON.stringify(S.recents));
      localStorage.setItem(LAST_KEY, path);
    } catch {
      /* storage disabled; recents just won't persist */
    }
  }

  /** Point the whole app at *path*: explorer, terminal cwd, git, chat target. */
  async function openFolder(path) {
    if (!path) return;
    try {
      await api(`/api/files?path=${encodeURIComponent(path)}&limit=1`);
    } catch (err) {
      toast(`cannot open ${base(path)}: ${err.message}`, true);
      S.recents = S.recents.filter((p) => p !== path);
      try {
        localStorage.setItem(RECENTS_KEY, JSON.stringify(S.recents));
      } catch {
        /* nothing to do */
      }
      return;
    }
    S.folder = path;
    rememberFolder(path);
    EXPANDED.clear();
    renderTargets();
    $('titlebar-source').textContent = path;
    $('panel-cwd').textContent = path;
    post('/api/search/reindex', { root: path }).catch(() => {});
    await refreshGit();
    showView(S.activeView === 'files' ? 'files' : S.activeView);
    // The welcome pane names the open folder and lists recents, so it has to be
    // redrawn when the folder changes — not only when a tab closes onto it.
    if (!S.activeTab) renderWelcome();
    if (S.panelOpen) window.Term.open(path);
    // Attach the conversation for this folder now. A chat costs nothing until
    // its first turn — no `claude` process is spawned — so the panel may as
    // well be live and showing the right folder from the moment you open one.
    showChat(path).catch(() => {
      /* the composer says what went wrong when you try to use it */
    });
  }

  async function pickFolder() {
    const picked = await window.descant.openFolder();
    if (picked) await openFolder(picked);
  }

  /** The folder picker in the composer row — recents, plus a way to add one. */
  function renderTargets() {
    const sel = $('composer-target');
    sel.innerHTML =
      S.recents
        .map((p) => `<option value="${esc(p)}">${esc(base(p))}</option>`)
        .join('') + `<option value="__open__">Open folder…</option>`;
    if (S.folder) sel.value = S.folder;
    updateComposerHint();
  }

  function updateComposerHint() {
    // The chips already say which folder, mode and model, and the button row
    // has no space left. What is worth saying is how to send — Enter-sends is a
    // choice, not a convention — so it goes in the field itself, where it is
    // visible exactly while there is nothing to lose by reading it.
    $('composer-input').placeholder = S.folder
      ? 'Ask Claude  ·  ⏎ send, ⇧⏎ newline'
      : 'Open a folder to start a conversation';
  }

  // ======================================================================
  // tabs
  // ======================================================================

  function renderTabs() {
    const host = $('tabs');
    host.innerHTML = '';
    for (const t of S.tabs) {
      const node = document.createElement('div');
      node.className = 'tab' + (t.key === S.activeTab ? ' active' : '');
      node.title = t.title || t.label;
      node.innerHTML = `<span>${esc(t.label)}</span>${
        t.dirty ? '<span class="dirty" title="Unsaved changes"></span>' : ''
      }<span class="close">${window.Icons.close}</span>`;
      node.addEventListener('click', (e) => {
        if (e.target.closest('.close')) {
          closeTab(t.key);
          return;
        }
        activateTab(t.key);
      });
      // Middle-click closes, the way every editor with tabs does it.
      node.addEventListener('auxclick', (e) => {
        if (e.button === 1) {
          e.preventDefault();
          closeTab(t.key);
        }
      });
      window.ContextMenu.attach(node, () => [
        { label: 'Close', hint: `${MOD}+W`, action: () => closeTab(t.key) },
        {
          label: 'Close Others',
          disabled: S.tabs.length < 2,
          action: () => {
            for (const other of [...S.tabs]) if (other.key !== t.key) closeTab(other.key);
          },
        },
        {
          label: 'Close All',
          action: () => {
            for (const other of [...S.tabs]) closeTab(other.key);
          },
        },
        { separator: true },
        {
          label: 'Copy Path',
          disabled: !t.payload?.path,
          action: () => navigator.clipboard.writeText(t.payload.path),
        },
      ]);
      host.appendChild(node);
    }
  }

  function openTab(tab) {
    const existing = S.tabs.find((t) => t.key === tab.key);
    if (existing) Object.assign(existing, tab);
    else S.tabs.push(tab);
    activateTab(tab.key);
  }

  function activateTab(key) {
    S.activeTab = key;
    const tab = S.tabs.find((t) => t.key === key);
    renderTabs();
    for (const v of document.querySelectorAll('.view')) v.classList.remove('active');
    if (!tab) {
      showLanguage(null);
      $('view-welcome').classList.add('active');
      renderWelcome();
      return;
    }
    if (tab.view === 'panel') {
      showLanguage(null);
      $('view-panel-generic').classList.add('active');
      tab.render($('view-panel-generic'));
      return;
    }
    $('view-editor').classList.add('active');
    if (tab.view === 'editor') {
      clearTimeout(autoSaveTimer);
      S.openFilePath = tab.payload.path;
      $('titlebar-context').textContent = relativeTo(tab.payload.path);
    }
    window.Editor.layout();
    // Monaco hosts one editor at a time, so re-open when the tab changes what
    // should be showing.
    const wanted = tab.view === 'diff' ? `diff:${tab.payload.key || tab.payload.path}` : tab.payload.path;
    if (window.Editor.current() !== wanted) {
      const p =
        tab.view === 'diff'
          ? window.Editor.openDiff(
              tab.payload.path,
              tab.payload.before,
              tab.payload.after,
              tab.payload.key
            )
          : // No text argument: the model is already loaded, and handing over a
            // snapshot here would overwrite unsaved edits.
            window.Editor.openFile(tab.payload.path);
      p.then(() => showLanguage(tab.payload.path)).catch((e) =>
        toast(`editor: ${e.message}`, true)
      );
    } else {
      showLanguage(tab.payload.path);
    }
  }

  function closeTab(key) {
    const i = S.tabs.findIndex((t) => t.key === key);
    if (i < 0) return;
    // Ask *before* anything is torn down: disposing the model first would make
    // the dirty check say "clean" and discard the edits without a word.
    const isFile = key.startsWith('file:');
    const path = isFile ? key.slice(5) : null;
    const stillDirty = isFile ? window.Editor.isPathDirty(path) : S.tabs[i].dirty;
    if (stillDirty && !confirm(`${S.tabs[i].label} has unsaved changes. Close anyway?`)) return;
    if (isFile) {
      clearTimeout(autoSaveTimer);
      window.Editor.closeFile(path);
      S.truncatedFiles.delete(path);
      if (S.openFilePath === path) S.openFilePath = null;
    }
    S.tabs.splice(i, 1);
    activateTab(S.tabs.length ? S.tabs[Math.max(0, i - 1)].key : null);
  }

  function relativeTo(path) {
    if (!path) return '';
    if (S.folder && path.startsWith(S.folder + '/')) return path.slice(S.folder.length + 1);
    return path;
  }

  // ======================================================================
  // editor plumbing
  // ======================================================================

  /** Status-bar language indicator, straight from Monaco's own detection. */
  function showLanguage(path) {
    const node = $('status-lang');
    if (!path) {
      node.textContent = '';
      return;
    }
    const id = window.Editor.langFor(path);
    node.textContent = id === 'plaintext' ? 'Plain Text' : id;
    node.title = `${window.Editor.languageCount()} languages available`;
  }

  let autoSaveTimer = null;

  /**
   * Autosave, if the setting asks for it.
   *
   * Off by default on purpose: this app exists alongside agents that read and
   * write the same files, and a buffer that saves itself mid-edit is a
   * surprise. When it is on, the debounce restarts on every keystroke so a save
   * lands in a pause, not mid-word.
   */
  function scheduleAutoSave(path) {
    const cfg = window.Settings.get();
    clearTimeout(autoSaveTimer);
    if (cfg.autoSave !== 'delay' || !path) return;
    autoSaveTimer = setTimeout(() => saveOpenFile({ quiet: true, path }), cfg.autoSaveDelay);
  }

  function autoSaveOnBlur() {
    if (window.Settings.get().autoSave !== 'blur') return;
    saveOpenFile({ quiet: true, path: window.Editor.current() });
  }

  /** Mark a tab as having unsaved changes, without redrawing the whole bar. */
  function markTabDirty(key, dirty) {
    const tab = S.tabs.find((t) => t.key === key);
    if (!tab || tab.dirty === dirty) return;
    tab.dirty = dirty;
    renderTabs();
  }

  async function saveOpenFile({ quiet = false, path = null } = {}) {
    // The path is decided by the caller and re-checked here. An autosave
    // scheduled for one file must never land on whatever is open when the timer
    // happens to fire — that is how a buffer ends up written into another
    // file's path during a tab switch.
    path = path || S.openFilePath;
    if (!path) {
      if (!quiet) toast('no file open to save');
      return;
    }
    if (S.truncatedFiles.has(path)) {
      toast('refusing to save: this file was too large to load in full', true);
      return;
    }
    if (!window.Editor.isPathDirty(path)) {
      if (!quiet) toast('no changes to save');
      return;
    }
    const text = window.Editor.textFor(path);
    if (text == null) {
      if (!quiet) toast('nothing to save');
      return;
    }
    try {
      await api('/api/file', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path, text, root: S.folder || '' }),
      });
      window.Editor.markSaved(text, path);
      markTabDirty(`file:${path}`, false);
      if (!quiet) toast(`saved ${base(path)}`);
      refreshGit();
    } catch (err) {
      // A failure is never quiet — silently not saving is the worst outcome.
      toast(`could not save: ${err.message}`, true);
    }
  }

  async function openFile(path, label, { line = null } = {}) {
    try {
      const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
      if (data.binary) {
        toast('binary file — not opening');
        return;
      }
      if (data.truncated) {
        S.truncatedFiles.add(path);
        toast(`${base(path)} is too large to load in full — opened read-only`, true);
      } else {
        S.truncatedFiles.delete(path);
      }
      // Load the buffer *before* opening the tab. openTab activates the tab
      // synchronously, and that activation also asks the editor to show this
      // path — if the model does not exist yet, the two race and the file can
      // open blank.
      clearTimeout(autoSaveTimer);
      S.openFilePath = path;
      await window.Editor.openFile(path, data.text, {
        readOnly: Boolean(data.truncated),
        onRun: runOpenFile,
        onSave: saveOpenFile,
        onDirty: (dirty) => {
          // `Editor.current()` rather than the captured `path`: these handlers
          // outlive the open that installed them, because the editor is reused.
          const active = window.Editor.current();
          markTabDirty(`file:${active}`, dirty);
          if (dirty) scheduleAutoSave(active);
        },
        onBlur: autoSaveOnBlur,
      });
      openTab({
        key: `file:${path}`,
        label: label || base(path),
        title: path,
        view: 'editor',
        // Only the path: the buffer lives in Monaco's model from here on. A
        // text snapshot in the tab would go stale the moment you typed, and
        // re-seeding from it on a tab switch is how edits used to disappear.
        payload: { path },
      });
      if (line) window.Editor.reveal(line);
      refreshRunnable(path);
    } catch (err) {
      toast(`could not open file: ${err.message}`, true);
    }
  }

  // ======================================================================
  // running the open file
  // ======================================================================
  //
  // The arrow in the title bar runs whatever file the editor is showing, with
  // the interpreter the *folder* implies — a project with a .venv gets its own
  // python, not whatever is first on PATH. The command goes into the terminal
  // rather than a hidden subprocess, so you can see it, edit it, and re-run it.

  let runnable = null; // {command, cwd} for the file currently open

  async function refreshRunnable(path) {
    const btn = $('btn-run-file');
    runnable = null;
    if (!path) {
      btn.disabled = true;
      btn.title = 'Open a file to run it';
      return;
    }
    try {
      const res = await api(
        `/api/runner?path=${encodeURIComponent(path)}&repo=${encodeURIComponent(S.folder || '')}`
      );
      if (!res.command) {
        btn.disabled = true;
        btn.title = res.reason || 'no runner for this file type';
        return;
      }
      runnable = res;
      btn.disabled = false;
      btn.title = `${res.command}   (${MOD}+Enter)`;
    } catch {
      btn.disabled = true;
      btn.title = 'no runner for this file';
    }
  }

  async function runOpenFile() {
    if (!runnable) {
      toast(S.openFilePath ? 'no runner for this file type' : 'open a file to run it');
      return;
    }
    await sendToTerminal(runnable.cwd, runnable.command);
  }

  /** Open the terminal on *cwd* and type *command* into it. */
  async function sendToTerminal(cwd, command) {
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
  }

  // ======================================================================
  // file explorer — a real tree
  // ======================================================================
  //
  // Expands a directory at a time, remembers what you opened, and can create,
  // rename, duplicate and delete.

  const EXPANDED = new Set(); // absolute dir paths currently open

  async function showFiles() {
    const host = $('sidebar-scroll');
    $('sidebar-title').textContent = 'Explorer';
    if (!S.folder) {
      host.innerHTML = '';
      host.appendChild(openFolderPrompt());
      return;
    }
    host.innerHTML = `<div class="empty-state"><p>Loading ${esc(base(S.folder))}…</p></div>`;
    try {
      const tree = document.createElement('div');
      tree.className = 'tree';
      await renderDir(tree, S.folder, 0);
      host.innerHTML = '';
      host.appendChild(treeToolbar(S.folder));
      host.appendChild(tree);
      if (!tree.children.length) {
        host.appendChild(el(`<div class="empty-state"><p>Nothing here yet.</p></div>`));
      }
    } catch (err) {
      host.innerHTML = `<div class="empty-state"><p>${esc(err.message)}</p></div>`;
    }
  }

  function openFolderPrompt() {
    const wrap = el(`
      <div class="empty-state">
        <p>No folder open.</p>
        <button class="btn small" id="btn-open-folder-side">Open Folder…</button>
      </div>`);
    wrap.querySelector('button').addEventListener('click', pickFolder);
    return wrap;
  }

  function treeToolbar(root) {
    const bar = el(`
      <div class="tree-toolbar">
        <span class="tree-root" title="${esc(root)}">${esc(base(root))}</span>
        <button class="tree-act" data-act="file" title="New file">+ File</button>
        <button class="tree-act" data-act="folder" title="New folder">+ Folder</button>
      </div>`);
    bar.querySelector('[data-act="file"]').addEventListener('click', () => createEntry(root, false));
    bar
      .querySelector('[data-act="folder"]')
      .addEventListener('click', () => createEntry(root, true));
    window.ContextMenu.attach(bar, () => [
      { label: 'Open Folder…', hint: `${MOD}+O`, action: pickFolder },
      { label: 'Reveal in Terminal', action: () => revealInTerminal({ path: root, dir: true }) },
      { label: 'Copy Path', action: () => navigator.clipboard.writeText(root) },
    ]);
    return bar;
  }

  /** Render one directory's children into *container*, indented by *depth*. */
  async function renderDir(container, dir, depth) {
    const showHidden = window.Settings.get().showHidden;
    const data = await api(
      `/api/files?path=${encodeURIComponent(dir)}&hidden=${showHidden ? 'true' : 'false'}`
    );
    for (const entry of data.entries) {
      container.appendChild(await renderEntry(entry, depth));
    }
    if (data.truncated) {
      container.appendChild(
        el(
          `<div class="tree-note" style="padding-left:${
            12 + depth * 12
          }px">…too many entries to show</div>`
        )
      );
    }
  }

  async function renderEntry(entry, depth) {
    const wrap = document.createElement('div');
    const isOpen = entry.dir && EXPANDED.has(entry.path);

    const row = el(`
      <div class="tree-row${entry.dir ? ' is-dir' : ''}" title="${esc(entry.path)}">
        <span class="twist">${
          entry.dir ? window.Icons[isOpen ? 'chevronDown' : 'chevronRight'] : ''
        }</span>
        <span class="ticon">${
          entry.dir ? window.FileIcons.forFolder(isOpen) : window.FileIcons.forFile(entry.name)
        }</span>
        <span class="tname">${esc(entry.name)}</span>
        <span class="tsize">${entry.dir ? '' : fmtBytes(entry.size)}</span>
        ${
          entry.dir
            ? `<button class="row-add" data-kind="file" title="New file in here">+</button>
               <button class="row-add" data-kind="folder" title="New folder in here">&#8862;</button>`
            : ''
        }
        <button class="row-delete" title="Delete">${window.Icons.close}</button>
      </div>`);
    row.style.paddingLeft = `${8 + depth * 12}px`;

    const kids = document.createElement('div');
    kids.className = 'tree-kids';

    row.querySelector('.row-delete').addEventListener('click', (e) => {
      e.stopPropagation();
      deleteEntry(entry);
    });

    for (const add of row.querySelectorAll('.row-add')) {
      add.addEventListener('click', async (e) => {
        e.stopPropagation();
        // Creating inside a closed folder should show you where it went.
        if (!EXPANDED.has(entry.path)) {
          EXPANDED.add(entry.path);
          row.querySelector('.twist').innerHTML = window.Icons.chevronDown;
          row.querySelector('.ticon').innerHTML = window.FileIcons.forFolder(true);
          kids.innerHTML = '';
          try {
            await renderDir(kids, entry.path, depth + 1);
          } catch {
            /* the inline input still works even if listing failed */
          }
        }
        createEntry(entry.path, add.dataset.kind === 'folder', kids, depth + 1);
      });
    }

    row.addEventListener('click', async () => {
      if (!entry.dir) {
        openFile(entry.path, entry.name);
        return;
      }
      if (EXPANDED.has(entry.path)) {
        EXPANDED.delete(entry.path);
        kids.innerHTML = '';
        row.querySelector('.twist').innerHTML = window.Icons.chevronRight;
        row.querySelector('.ticon').innerHTML = window.FileIcons.forFolder(false);
        return;
      }
      EXPANDED.add(entry.path);
      row.querySelector('.twist').innerHTML = window.Icons.chevronDown;
      row.querySelector('.ticon').innerHTML = window.FileIcons.forFolder(true);
      try {
        await renderDir(kids, entry.path, depth + 1);
      } catch (err) {
        toast(err.message, true);
      }
    });

    window.ContextMenu.attach(row, () => [
      entry.dir
        ? { label: 'New File…', action: () => createEntry(entry.path, false, kids, depth + 1) }
        : { label: 'Open', action: () => openFile(entry.path, entry.name) },
      entry.dir
        ? { label: 'New Folder…', action: () => createEntry(entry.path, true, kids, depth + 1) }
        : {
            label: 'Run',
            hint: `${MOD}+↵`,
            action: async () => {
              await openFile(entry.path, entry.name);
              runOpenFile();
            },
          },
      { separator: true },
      { label: 'Rename…', action: () => renameEntry(entry, row, depth) },
      { label: 'Duplicate', action: () => duplicateEntry(entry) },
      { separator: true },
      { label: 'Copy Path', action: () => navigator.clipboard.writeText(entry.path) },
      { label: 'Copy Relative Path', action: () => navigator.clipboard.writeText(relativeTo(entry.path)) },
      { label: 'Reveal in Terminal', action: () => revealInTerminal(entry) },
      { separator: true },
      { label: 'Delete', danger: true, action: () => deleteEntry(entry) },
    ]);

    wrap.appendChild(row);
    wrap.appendChild(kids);
    if (isOpen) await renderDir(kids, entry.path, depth + 1);
    return wrap;
  }

  /** Drop the terminal into a folder (or a file's folder). */
  async function revealInTerminal(entry) {
    const dir = entry.dir ? entry.path : entry.path.replace(/\/[^/]+$/, '');
    await sendToTerminal(S.folder || dir, `cd ${JSON.stringify(dir)}`);
  }

  /**
   * Ask for a name inline, in the tree.
   *
   * Not `prompt()`: Electron removed it ("prompt() is and will not be
   * supported"), so it throws rather than asking. An input row in the tree is
   * what a file explorer should do anyway — you can see where the thing is
   * about to land.
   */
  function askForName({ anchor, depth, directory, value = '', placeholder, onName, replace = null }) {
    const row = el(`
      <div class="tree-row tree-new" style="padding-left:${8 + depth * 12}px">
        <span class="twist"></span>
        <span class="ticon">${
          directory ? window.FileIcons.forFolder(false) : window.FileIcons.forFile(value || 'x')
        }</span>
        <input class="tname-input" spellcheck="false" placeholder="${esc(
          placeholder || (directory ? 'folder name' : 'name.ext — main.py, index.html')
        )}" />
      </div>`);
    if (replace) replace.replaceWith(row);
    else anchor.prepend(row);
    const input = row.querySelector('.tname-input');
    const icon = row.querySelector('.ticon');
    input.value = value;
    let done = false;

    const finish = (name) => {
      if (done) return;
      done = true;
      if (replace) row.replaceWith(replace);
      else row.remove();
      if (name && name !== value) onName(name);
    };

    // Show the icon the file will actually get, as you type the extension.
    if (!directory) {
      input.addEventListener('input', () => {
        icon.innerHTML = window.FileIcons.forFile(input.value || 'x');
      });
    }
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') finish(input.value.trim());
      if (e.key === 'Escape') finish(null);
      e.stopPropagation();
    });
    input.addEventListener('blur', () => finish(input.value.trim()));
    input.focus();
    // Select the stem, not the extension — renaming usually keeps the type.
    const dot = value.lastIndexOf('.');
    if (dot > 0) input.setSelectionRange(0, dot);
    else input.select();
  }

  /** Create a file or folder under *dir*. The name carries the extension. */
  function createEntry(dir, directory, anchor = null, depth = 0) {
    const host = anchor || document.querySelector('#sidebar-scroll .tree');
    if (!host) return;
    askForName({
      anchor: host,
      depth,
      directory,
      onName: async (name) => {
        try {
          const res = await post('/api/fs/create', {
            root: S.folder,
            path: dir === S.folder ? name : `${dir}/${name}`,
            directory,
          });
          toast(`created ${res.name}`);
          if (dir !== S.folder) EXPANDED.add(dir);
          await showFiles();
          if (!directory) openFile(res.path, res.name);
          refreshGit();
        } catch (err) {
          toast(`could not create: ${err.message}`, true);
        }
      },
    });
  }

  function renameEntry(entry, row, depth) {
    askForName({
      depth,
      directory: entry.dir,
      value: entry.name,
      replace: row,
      onName: async (name) => {
        try {
          const res = await post('/api/fs/rename', {
            root: S.folder,
            path: entry.path,
            to: name,
          });
          // A renamed file's tab and buffer are keyed on the old path, so the
          // honest thing is to close it rather than leave a tab that saves to a
          // file that no longer exists.
          const tabKey = `file:${entry.path}`;
          if (S.tabs.some((t) => t.key === tabKey)) {
            window.Editor.closeFile(entry.path);
            S.tabs = S.tabs.filter((t) => t.key !== tabKey);
            renderTabs();
          }
          toast(`renamed to ${res.name}`);
          await showFiles();
          if (!entry.dir) openFile(res.path, res.name);
          refreshGit();
        } catch (err) {
          toast(`could not rename: ${err.message}`, true);
        }
      },
    });
  }

  async function duplicateEntry(entry) {
    try {
      const res = await post('/api/fs/duplicate', { root: S.folder, path: entry.path });
      toast(`created ${res.name}`);
      await showFiles();
      refreshGit();
    } catch (err) {
      toast(`could not duplicate: ${err.message}`, true);
    }
  }

  async function deleteEntry(entry) {
    const kind = entry.dir ? 'folder' : 'file';
    if (!confirm(`Delete this ${kind}?\n\n${entry.path}\n\nThis cannot be undone.`)) return;
    try {
      await post('/api/fs/delete', { root: S.folder, path: entry.path, recursive: false });
    } catch (err) {
      // A non-empty folder is a second question, not a failure.
      if (entry.dir && /not empty/.test(err.message)) {
        if (!confirm(`${entry.name} is not empty. Delete it and everything inside?`)) return;
        try {
          await post('/api/fs/delete', { root: S.folder, path: entry.path, recursive: true });
        } catch (err2) {
          toast(`could not delete: ${err2.message}`, true);
          return;
        }
      } else {
        toast(`could not delete: ${err.message}`, true);
        return;
      }
    }
    EXPANDED.delete(entry.path);
    const tabKey = `file:${entry.path}`;
    if (S.tabs.some((t) => t.key === tabKey)) {
      window.Editor.closeFile(entry.path);
      S.tabs = S.tabs.filter((t) => t.key !== tabKey);
      activateTab(S.tabs.length ? S.tabs[S.tabs.length - 1].key : null);
    }
    toast(`deleted ${entry.name}`);
    await showFiles();
    refreshGit();
  }

  // ======================================================================
  // search
  // ======================================================================

  const SEARCH = { query: '', include: '', case: false, word: false, regex: false, open: new Set() };
  let searchTimer = null;

  function showSearch() {
    const host = $('sidebar-scroll');
    $('sidebar-title').textContent = 'Search';
    if (!S.folder) {
      host.innerHTML = '';
      host.appendChild(openFolderPrompt());
      return;
    }
    host.innerHTML = '';
    const panel = el(`
      <div class="search-panel">
        <input class="search-input" id="search-q" spellcheck="false" placeholder="Search" />
        <div class="search-opts">
          <button class="opt" data-opt="case" title="Match case">Aa</button>
          <button class="opt" data-opt="word" title="Match whole word">ab|</button>
          <button class="opt" data-opt="regex" title="Use regular expression">.*</button>
        </div>
        <input class="search-input small" id="search-include" spellcheck="false"
               placeholder="files to include — *.py, src/**" />
        <div class="search-summary" id="search-summary"></div>
        <div class="search-results" id="search-results"></div>
      </div>`);
    host.appendChild(panel);

    const q = panel.querySelector('#search-q');
    const include = panel.querySelector('#search-include');
    q.value = SEARCH.query;
    include.value = SEARCH.include;
    for (const btn of panel.querySelectorAll('.opt')) {
      btn.classList.toggle('on', SEARCH[btn.dataset.opt]);
      btn.addEventListener('click', () => {
        SEARCH[btn.dataset.opt] = !SEARCH[btn.dataset.opt];
        btn.classList.toggle('on', SEARCH[btn.dataset.opt]);
        runSearch();
      });
    }
    const debounced = () => {
      SEARCH.query = q.value;
      SEARCH.include = include.value;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(runSearch, 220);
    };
    q.addEventListener('input', debounced);
    include.addEventListener('input', debounced);
    q.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        clearTimeout(searchTimer);
        runSearch();
      }
    });
    q.focus();
    if (SEARCH.query) runSearch();
  }

  async function runSearch() {
    const results = $('search-results');
    const summary = $('search-summary');
    if (!results) return;
    if (!SEARCH.query) {
      results.innerHTML = '';
      summary.textContent = '';
      return;
    }
    summary.textContent = 'searching…';
    const qs = new URLSearchParams({
      root: S.folder,
      q: SEARCH.query,
      regex: String(SEARCH.regex),
      case: String(SEARCH.case),
      word: String(SEARCH.word),
      include: SEARCH.include,
    });
    let data;
    try {
      data = await api(`/api/search/text?${qs}`);
    } catch (err) {
      summary.textContent = err.message;
      results.innerHTML = '';
      return;
    }
    summary.textContent = data.matches
      ? `${data.matches} result${data.matches === 1 ? '' : 's'} in ${data.files.length} file${
          data.files.length === 1 ? '' : 's'
        }${data.truncated ? ' (truncated)' : ''}`
      : 'No results';
    results.innerHTML = '';
    for (const file of data.files) {
      const open = SEARCH.open.has(file.path) || data.files.length <= 12;
      const group = el(`
        <div class="sr-group${open ? ' open' : ''}">
          <div class="sr-head">
            <span class="chevron">${window.Icons[open ? 'chevronDown' : 'chevronRight']}</span>
            <span class="ticon">${window.FileIcons.forFile(base(file.path))}</span>
            <span class="sr-path" title="${esc(file.path)}">${esc(base(file.path))}</span>
            <span class="sr-dir">${esc(file.path.split('/').slice(0, -1).join('/'))}</span>
            <span class="count">${file.count}</span>
          </div>
          <div class="sr-rows"></div>
        </div>`);
      group.querySelector('.sr-head').addEventListener('click', () => {
        const nowOpen = !group.classList.contains('open');
        group.classList.toggle('open', nowOpen);
        group.querySelector('.chevron').innerHTML =
          window.Icons[nowOpen ? 'chevronDown' : 'chevronRight'];
        if (nowOpen) SEARCH.open.add(file.path);
        else SEARCH.open.delete(file.path);
      });
      const rows = group.querySelector('.sr-rows');
      for (const m of file.matches) {
        const before = m.text.slice(0, m.column);
        const hit = m.text.slice(m.column, m.end);
        const after = m.text.slice(m.end);
        const row = el(`
          <div class="sr-row" title="line ${m.line}">
            <span class="sr-line">${m.line}</span>
            <span class="sr-text">${esc(before.trimStart())}<mark>${esc(hit)}</mark>${esc(
              after
            )}</span>
          </div>`);
        row.addEventListener('click', () =>
          openFile(`${S.folder}/${file.path}`, base(file.path), { line: m.line })
        );
        rows.appendChild(row);
      }
      results.appendChild(group);
    }
  }

  // ======================================================================
  // source control
  // ======================================================================

  async function refreshGit() {
    if (!S.folder) {
      S.git = null;
      paintBranch();
      return;
    }
    try {
      S.git = await api(`/api/git/status?repo=${encodeURIComponent(S.folder)}`);
    } catch {
      S.git = null;
    }
    paintBranch();
    if (S.activeView === 'scm') showScm();
    renderActivityBar();
  }

  function paintBranch() {
    const node = $('status-branch');
    if (!S.git || !S.git.is_repo) {
      node.textContent = S.folder ? 'not a repo' : '';
      node.title = '';
      return;
    }
    const divergence = [
      S.git.behind ? `↓${S.git.behind}` : '',
      S.git.ahead ? `↑${S.git.ahead}` : '',
    ]
      .filter(Boolean)
      .join(' ');
    node.textContent = `${S.git.branch}${divergence ? ' ' + divergence : ''}`;
    node.title = S.git.upstream ? `tracking ${S.git.upstream}` : 'no upstream';
  }

  async function showScm() {
    const host = $('sidebar-scroll');
    $('sidebar-title').textContent = 'Source Control';
    if (!S.folder) {
      host.innerHTML = '';
      host.appendChild(openFolderPrompt());
      return;
    }
    if (!S.git) S.git = await api(`/api/git/status?repo=${encodeURIComponent(S.folder)}`);

    host.innerHTML = '';
    if (!S.git.is_repo) {
      const prompt = el(`
        <div class="empty-state">
          <p><code>${esc(base(S.folder))}</code> is not a git repository.</p>
          <button class="btn small">git init</button>
        </div>`);
      prompt.querySelector('button').addEventListener('click', async () => {
        try {
          await post('/api/git/init', { repo: S.folder });
          toast('initialised a repository');
          await refreshGit();
          showScm();
        } catch (err) {
          toast(err.message, true);
        }
      });
      host.appendChild(prompt);
      return;
    }

    host.appendChild(scmToolbar());
    host.appendChild(commitBox());
    const staged = S.git.staged || [];
    const changes = S.git.changes || [];
    if (!staged.length && !changes.length) {
      host.appendChild(el('<div class="empty-state"><p>No changes.</p></div>'));
    }
    if (staged.length) host.appendChild(changeGroup('Staged Changes', staged, true));
    if (changes.length) host.appendChild(changeGroup('Changes', changes, false));
  }

  function scmToolbar() {
    const bar = el(`
      <div class="tree-toolbar scm-toolbar">
        <span class="tree-root" title="${esc(S.git.upstream || 'no upstream')}">${esc(
          S.git.branch
        )}</span>
        <button class="tree-act" data-act="branch" title="Switch branch">Branch</button>
        <button class="tree-act" data-act="pull" title="Pull (fast-forward only)">Pull</button>
        <button class="tree-act" data-act="push" title="Push to origin">Push</button>
      </div>`);
    const act = async (name, fn) => {
      const btn = bar.querySelector(`[data-act="${name}"]`);
      btn.disabled = true;
      try {
        const res = await fn();
        toast(res.output ? res.output.split('\n').slice(-1)[0] : `${name} done`);
      } catch (err) {
        toast(`${name} failed: ${err.message}`, true);
      } finally {
        btn.disabled = false;
        await refreshGit();
      }
    };
    bar
      .querySelector('[data-act="pull"]')
      .addEventListener('click', () => act('pull', () => post('/api/git/pull', { repo: S.folder })));
    bar
      .querySelector('[data-act="push"]')
      .addEventListener('click', () => act('push', () => post('/api/git/push', { repo: S.folder })));
    bar.querySelector('[data-act="branch"]').addEventListener('click', showBranchMenu);
    return bar;
  }

  async function showBranchMenu(e) {
    let data;
    try {
      data = await api(`/api/git/branches?repo=${encodeURIComponent(S.folder)}`);
    } catch (err) {
      toast(err.message, true);
      return;
    }
    const checkout = async (name, create) => {
      try {
        await post('/api/git/checkout', { repo: S.folder, name, create });
        toast(`on ${name}`);
      } catch (err) {
        toast(err.message, true);
      }
      await refreshGit();
      if (S.activeView === 'files') showFiles();
    };
    const items = [
      // Electron has no prompt(), so the name is asked for with the same inline
      // input the file tree uses.
      { label: 'New Branch…', action: () => askBranchName((value) => checkout(value, true)) },
      { separator: true },
      ...data.local.slice(0, 15).map((b) => ({
        label: (b.name === data.current ? '● ' : '　') + b.name,
        hint: b.when,
        action: () => checkout(b.name, false),
      })),
    ];
    window.ContextMenu.show(e.clientX, e.clientY, items);
  }

  function askBranchName(onName) {
    const host = $('sidebar-scroll');
    askForName({
      anchor: host,
      depth: 0,
      directory: false,
      placeholder: 'new branch name',
      onName,
    });
    host.scrollTop = 0;
  }

  function commitBox() {
    const box = el(`
      <div class="commit-box">
        <textarea id="commit-msg" rows="2" spellcheck="false"
                  placeholder="Message (${MOD}+Enter to commit)"></textarea>
        <div class="commit-row">
          <button class="btn small secondary" id="btn-stage-all">Stage All</button>
          <button class="btn small" id="btn-commit">Commit</button>
        </div>
      </div>`);
    const doCommit = async () => {
      const msg = box.querySelector('#commit-msg').value.trim();
      if (!msg) {
        toast('a commit message is required');
        return;
      }
      const hasStaged = (S.git.staged || []).length > 0;
      try {
        await post('/api/git/commit', {
          repo: S.folder,
          message: msg,
          // Committing with nothing staged should do what the button implies
          // rather than erroring: stage the tracked edits and commit those.
          stage_all: !hasStaged,
        });
        toast('committed');
        box.querySelector('#commit-msg').value = '';
      } catch (err) {
        toast(`commit failed: ${err.message}`, true);
      }
      await refreshGit();
      showScm();
    };
    box.querySelector('#btn-commit').addEventListener('click', doCommit);
    box.querySelector('#commit-msg').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        doCommit();
      }
    });
    box.querySelector('#btn-stage-all').addEventListener('click', async () => {
      try {
        await post('/api/git/stage', { repo: S.folder, paths: [] });
      } catch (err) {
        toast(err.message, true);
      }
      await refreshGit();
      showScm();
    });
    return box;
  }

  function changeGroup(title, files, staged) {
    const group = el(`
      <div class="repo-group">
        <div class="repo-header">
          <span class="chevron">${window.Icons.chevronDown}</span>
          <span class="repo-name">${esc(title)}</span>
          <span class="count">${files.length}</span>
        </div>
        <div class="repo-sessions"></div>
      </div>`);
    group.querySelector('.repo-header').addEventListener('click', () =>
      group.classList.toggle('collapsed')
    );
    const list = group.querySelector('.repo-sessions');
    for (const f of files) list.appendChild(changeRow(f, staged));
    return group;
  }

  function changeRow(f, staged) {
    const row = el(`
      <div class="tree-row change-row" title="${esc(f.path)}">
        <span class="ticon">${window.FileIcons.forFile(base(f.path))}</span>
        <span class="tname">${esc(base(f.path))}</span>
        <span class="tsize change-dir">${esc(f.path.split('/').slice(0, -1).join('/'))}</span>
        ${
          staged
            ? `<button class="row-add" data-act="unstage" title="Unstage">${window.Icons.minus}</button>`
            : `<button class="row-add" data-act="discard" title="Discard changes">${window.Icons.discard}</button>
               <button class="row-add" data-act="stage" title="Stage">${window.Icons.plus}</button>`
        }
        <span class="change-flag ${esc(f.label)}">${esc((f.label || '?')[0].toUpperCase())}</span>
      </div>`);

    row.addEventListener('click', () => openGitDiff(f, staged));

    const run = async (fn) => {
      try {
        await fn();
      } catch (err) {
        toast(err.message, true);
      }
      await refreshGit();
      showScm();
    };
    row.querySelector('[data-act="stage"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      run(() => post('/api/git/stage', { repo: S.folder, paths: [f.path] }));
    });
    row.querySelector('[data-act="unstage"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      run(() => post('/api/git/unstage', { repo: S.folder, paths: [f.path] }));
    });
    row.querySelector('[data-act="discard"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      const what = f.untracked ? `Delete ${f.path}?` : `Discard all changes to ${f.path}?`;
      if (!confirm(`${what}\n\nThis cannot be undone.`)) return;
      run(() => post('/api/git/discard', { repo: S.folder, paths: [f.path] }));
    });

    window.ContextMenu.attach(row, () => [
      { label: 'Open File', action: () => openFile(`${S.folder}/${f.path}`, base(f.path)) },
      { label: 'Open Changes', action: () => openGitDiff(f, staged) },
      { separator: true },
      staged
        ? {
            label: 'Unstage',
            action: () => run(() => post('/api/git/unstage', { repo: S.folder, paths: [f.path] })),
          }
        : {
            label: 'Stage',
            action: () => run(() => post('/api/git/stage', { repo: S.folder, paths: [f.path] })),
          },
      { label: 'Copy Path', action: () => navigator.clipboard.writeText(`${S.folder}/${f.path}`) },
      { separator: true },
      {
        label: f.untracked ? 'Delete File' : 'Discard Changes',
        danger: true,
        action: () => {
          if (!confirm(`Discard changes to ${f.path}?\n\nThis cannot be undone.`)) return;
          run(() => post('/api/git/discard', { repo: S.folder, paths: [f.path] }));
        },
      },
    ]);
    return row;
  }

  /** Side-by-side: what HEAD (or the index) has versus what you have. */
  async function openGitDiff(f, staged) {
    const full = `${S.folder}/${f.path}`;
    try {
      const sides = await api(
        `/api/git/diff?repo=${encodeURIComponent(S.folder)}&path=${encodeURIComponent(
          f.path
        )}&staged=${staged ? 'true' : 'false'}`
      );
      const key = `git:${staged ? 'index' : 'work'}:${f.path}`;
      openTab({
        key,
        label: `${base(f.path)} (${staged ? 'staged' : 'diff'})`,
        title: full,
        view: 'diff',
        payload: { path: full, before: sides.before, after: sides.after, key },
      });
    } catch (err) {
      toast(`could not diff: ${err.message}`, true);
    }
  }

  // ======================================================================
  // quick open  (Cmd+P)
  // ======================================================================

  const QUICK = { open: false, results: [], index: 0 };

  function toggleQuickOpen(force) {
    const want = force === undefined ? !QUICK.open : force;
    QUICK.open = want;
    let node = $('quickopen');
    if (!want) {
      node?.remove();
      return;
    }
    if (!S.folder) {
      toast('open a folder first');
      QUICK.open = false;
      return;
    }
    node = el(`
      <div id="quickopen">
        <input id="qo-input" spellcheck="false" placeholder="Go to file…" />
        <div id="qo-list"></div>
      </div>`);
    document.body.appendChild(node);
    const input = node.querySelector('#qo-input');
    input.addEventListener('input', () => queryQuickOpen(input.value));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') return toggleQuickOpen(false);
      if (e.key === 'ArrowDown' || (e.key === 'n' && e.ctrlKey)) {
        e.preventDefault();
        QUICK.index = Math.min(QUICK.index + 1, QUICK.results.length - 1);
        paintQuickOpen();
      }
      if (e.key === 'ArrowUp' || (e.key === 'p' && e.ctrlKey)) {
        e.preventDefault();
        QUICK.index = Math.max(QUICK.index - 1, 0);
        paintQuickOpen();
      }
      if (e.key === 'Enter') {
        const hit = QUICK.results[QUICK.index];
        if (hit) {
          toggleQuickOpen(false);
          openFile(`${S.folder}/${hit.path}`, base(hit.path));
        }
      }
    });
    node.addEventListener('pointerdown', (e) => {
      if (e.target === node) toggleQuickOpen(false);
    });
    input.focus();
    queryQuickOpen('');
  }

  async function queryQuickOpen(q) {
    try {
      const data = await api(
        `/api/search/files?root=${encodeURIComponent(S.folder)}&q=${encodeURIComponent(q)}`
      );
      QUICK.results = data.results;
      QUICK.index = 0;
      paintQuickOpen();
    } catch (err) {
      const list = $('qo-list');
      if (list) list.innerHTML = `<div class="qo-empty">${esc(err.message)}</div>`;
    }
  }

  function paintQuickOpen() {
    const list = $('qo-list');
    if (!list) return;
    if (!QUICK.results.length) {
      list.innerHTML = '<div class="qo-empty">No matching files</div>';
      return;
    }
    list.innerHTML = '';
    QUICK.results.forEach((r, i) => {
      const row = el(`
        <div class="qo-row${i === QUICK.index ? ' active' : ''}">
          <span class="ticon">${window.FileIcons.forFile(base(r.path))}</span>
          <span class="qo-name">${esc(base(r.path))}</span>
          <span class="qo-dir">${esc(r.path.split('/').slice(0, -1).join('/'))}</span>
        </div>`);
      row.addEventListener('click', () => {
        toggleQuickOpen(false);
        openFile(`${S.folder}/${r.path}`, base(r.path));
      });
      list.appendChild(row);
    });
    list.querySelector('.qo-row.active')?.scrollIntoView({ block: 'nearest' });
  }

  // ======================================================================
  // Chat — the agent panel *is* the conversation
  // ======================================================================

  const CHATS = new Map(); // cwd -> handle

  function activeChat() {
    return S.chatCwd ? CHATS.get(S.chatCwd) : null;
  }

  /** The chat for a folder, started if this is the first time we need it. */
  async function chatFor(cwd) {
    const existing = CHATS.get(cwd);
    if (existing) return existing;
    const chat = await post('/api/chats', {
      repo: cwd,
      permission_mode: window.Settings.get().permissionMode,
      model: window.Settings.get().model || '',
    });
    const handle = window.Chat.attach({
      chat,
      els: { log: $('agent-log') },
      api,
      wsUrl: (id) => `${S.api.replace(/^http/, 'ws')}/ws/chats/${id}`,
      onDiff: openAgentDiff,
      onOpenFile: (path) => openFile(path, base(path)),
      onMeta: (m) => {
        if (S.chatCwd === m.cwd) paintChatHeader(m);
      },
    });
    CHATS.set(cwd, handle);
    return handle;
  }

  /**
   * Repaint the panel's chrome from one chat's metadata.
   *
   * Everything the header and the chips say comes from the backend's view of
   * the conversation, never from what the UI last set — a mode change that
   * failed has to look like it failed.
   */
  function paintChatHeader(m) {
    const status = ['running', 'needs_input', 'failed'].includes(m.status) ? m.status : 'idle';
    $('agent-dot').className = `dot ${status}`;
    $('agent-title').textContent = `${m.repo_name || base(m.cwd)} · ${
      window.Chat.STATUS_LABEL[m.status] || m.status
    }`;
    $('agent-title').title = [
      `session ${m.session_id}`,
      `${m.turns} turn${m.turns === 1 ? '' : 's'}`,
      m.cost_usd ? `$${m.cost_usd.toFixed(4)}` : 'no cost reported yet',
      m.error || '',
    ]
      .filter(Boolean)
      .join(' · ');

    $('chip-mode').textContent = window.Chat.MODE_LABEL[m.permission_mode] || m.permission_mode;
    $('chip-mode').title = m.mode_help || 'Permission mode';
    $('chip-mode').classList.toggle('warn', m.permission_mode === 'dontAsk');
    $('chip-model').textContent = m.model || 'default';
    $('chip-model').title = m.model ? `--model ${m.model}` : 'whatever your Claude Code config says';

    const busy = m.status === 'running' || m.status === 'starting';
    $('btn-send').disabled = busy;
    $('btn-stop').style.display = busy ? '' : 'none';
    // A pending decision is the one thing that should pull your eye.
    $('agent-panel').classList.toggle('needs-you', m.status === 'needs_input');
  }

  /** Before a conversation exists, the chips show what the next one will use. */
  function paintIdleChips() {
    if (activeChat()) return;
    const cfg = window.Settings.get();
    $('chip-mode').textContent = window.Chat.MODE_LABEL[cfg.permissionMode] || cfg.permissionMode;
    $('chip-model').textContent = cfg.model || 'default';
  }

  /** Modes and models, from the backend so the menu can't offer a bad one. */
  async function chatOptions() {
    if (!S.chatOptions) S.chatOptions = await api('/api/chats/options');
    return S.chatOptions;
  }

  /** Below the control that opened it — a click with no pointer (keyboard,
   *  a test) still has to put the menu somewhere sensible. */
  function anchorOf(e) {
    const rect = (e.currentTarget || e.target).getBoundingClientRect();
    return [rect.left, rect.bottom + 4];
  }

  async function showModeMenu(e) {
    const [x, y] = anchorOf(e);
    const handle = activeChat();
    const { modes } = await chatOptions();
    const currentMode = handle?.meta.permission_mode || window.Settings.get().permissionMode;
    window.ContextMenu.show(
      x,
      y,
      modes.map((m) => ({
        label: (m.id === currentMode ? '● ' : '\u3000') + (window.Chat.MODE_LABEL[m.id] || m.id),
        hint: m.help,
        action: async () => {
          // Remember it as the default too: picking a mode here almost always
          // means "this is how I want to work", not "just this once".
          window.Settings.set({ permissionMode: m.id });
          if (handle) {
            try {
              await handle.setMode(m.id);
            } catch (err) {
              toast(err.message, true);
            }
          }
        },
      }))
    );
  }

  async function showModelMenu(e) {
    const [x, y] = anchorOf(e);
    const handle = activeChat();
    const { models } = await chatOptions();
    const currentModel = handle?.meta.model || window.Settings.get().model || '';
    window.ContextMenu.show(
      x,
      y,
      models.map((id) => ({
        label: (id === currentModel ? '● ' : '\u3000') + (id || 'default'),
        hint: id ? '' : 'your Claude Code config decides',
        action: async () => {
          window.Settings.set({ model: id });
          if (handle) {
            try {
              await handle.setModel(id);
            } catch (err) {
              toast(err.message, true);
            }
          }
        },
      }))
    );
  }

  /** Drop this folder's conversation and start a fresh one. */
  async function newChat() {
    if (!S.folder) {
      toast('open a folder first', true);
      return;
    }
    const old = CHATS.get(S.folder);
    if (old) {
      if (!confirm('Start a new conversation?\n\nThe current one is closed and its context is lost.'))
        return;
      old.dispose();
      CHATS.delete(S.folder);
      post(`/api/chats/${old.meta.chat_id}/close`, {}).catch(() => {});
    }
    $('agent-log').innerHTML = '';
    S.chatCwd = null;
    await showChat(S.folder);
    toast('new conversation');
  }

  async function showChat(cwd) {
    cwd = cwd || S.folder;
    if (!cwd) return;
    let handle;
    try {
      handle = await chatFor(cwd);
    } catch (err) {
      toast(`could not start a chat: ${err.message}`, true);
      return;
    }
    S.chatCwd = cwd;
    handle.repaint();
    paintChatHeader(handle.meta);
    updateComposerHint();
    $('composer-input').focus();
  }

  async function sendChat() {
    const text = $('composer-input').value.trim();
    if (!text) {
      toast('type something for Claude to do first');
      return;
    }
    if (!S.folder) {
      toast('open a folder first', true);
      return;
    }
    if (S.chatCwd !== S.folder) await showChat(S.folder);
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
    if (!S.folder) {
      toast('open a folder first', true);
      return;
    }
    await sendToTerminal(S.folder, command);
    $('composer-input').value = '';
  }

  async function stopChat() {
    const handle = activeChat();
    if (handle) await handle.interrupt();
  }

  /**
   * Show what an agent edit changed.
   *
   * The transcript records the edit, not the file's before/after state, so we
   * reconstruct: read the file as it stands now and apply the edit backwards.
   */
  async function openAgentDiff(ev) {
    const input = ev.tool_input || {};
    const path = input.file_path || input.path;
    if (!path) {
      toast('that edit has no file path to diff');
      return;
    }
    const label = base(path);
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
      before = after.replace(input.new_string, input.old_string);
    } else {
      before = input.old_string ?? '';
      after = input.new_string ?? after ?? '';
      toast(`${label} isn't on this machine — showing the edit without context`);
    }

    const key = `diff:${ev.tool_use_id || path}`;
    openTab({
      key,
      label: `${label} (diff)`,
      title: path,
      view: 'diff',
      payload: { path, before, after, key },
    });
  }

  // ======================================================================
  // activity bar, panel, views
  // ======================================================================

  const ACTIVITIES = [
    { id: 'files', icon: 'files', title: `Explorer (${MOD}+Shift+E)`, onSelect: showFiles },
    { id: 'search', icon: 'search', title: `Search (${MOD}+Shift+F)`, onSelect: showSearch },
    { id: 'scm', icon: 'scm', title: `Source Control (${MOD}+Shift+G)`, onSelect: showScm },
    { id: 'terminal', icon: 'terminal', title: `Terminal (${MOD}+\`)`, onSelect: () => togglePanel() },
    { id: 'settings', icon: 'settings', title: 'Settings', onSelect: showSettings },
  ];

  function showView(id) {
    const activity = ACTIVITIES.find((a) => a.id === id);
    if (!activity) return;
    S.activeView = id;
    renderActivityBar();
    activity.onSelect();
  }

  function renderActivityBar() {
    const host = $('activity-bar');
    host.innerHTML = '';
    for (const a of ACTIVITIES) {
      const btn = document.createElement('button');
      btn.className = 'activity-btn' + (S.activeView === a.id ? ' active' : '');
      btn.title = a.title;
      btn.innerHTML = window.Icons[a.icon];
      const changes = (S.git?.files || []).length;
      if (a.id === 'scm' && changes) {
        const b = document.createElement('span');
        b.className = 'badge';
        b.textContent = String(changes);
        btn.appendChild(b);
      }
      btn.addEventListener('click', () => {
        if (a.id === 'terminal') {
          a.onSelect();
          return;
        }
        showView(a.id);
      });
      host.appendChild(btn);
    }
  }

  function togglePanel(force) {
    S.panelOpen = force === undefined ? !S.panelOpen : force;
    $('panel').classList.toggle('hidden', !S.panelOpen);
    if (S.panelOpen) {
      $('panel-cwd').textContent = S.folder || '';
      window.Term.open(S.folder).then((res) => {
        if (res && !res.ok) toast(res.error, true);
      });
      setTimeout(() => window.Term.fitActive(), 30);
    }
    window.Editor.layout();
  }

  function showSettings() {
    openTab({
      key: 'settings',
      label: 'Settings',
      view: 'panel',
      render: (host) =>
        window.Settings.render(host, {
          config: S.config || {},
          folder: S.folder,
          onResetLayout: () => {
            window.Resize.reset();
            toast('pane sizes reset');
          },
          onClearChats: async () => {
            const open = [...CHATS.keys()];
            if (!open.length) {
              toast('no chats open');
              return;
            }
            if (!confirm(`Close ${open.length} conversation${open.length === 1 ? '' : 's'}?`)) return;
            for (const cwd of open) {
              const handle = CHATS.get(cwd);
              handle.dispose();
              CHATS.delete(cwd);
              post(`/api/chats/${handle.meta.chat_id}/close`, {}).catch(() => {});
            }
            S.chatCwd = null;
            $('agent-log').innerHTML = '';
            toast(`closed ${open.length}`);
          },
        }),
    });
  }

  /** Push settings that other modules own into those modules. */
  function applySettings(cfg) {
    window.Editor.applySettings?.(cfg);
    window.Term.setFontSize?.(cfg.fontSize);
    paintIdleChips();
    if (S.activeView === 'files') showFiles();
  }

  function renderWelcome() {
    const host = $('view-welcome');
    host.innerHTML = '';
    const wrap = el(`
      <div class="empty-state">
        <h2>${S.folder ? esc(base(S.folder)) : 'No folder open'}</h2>
        <p>${
          S.folder
            ? 'Pick a file from the explorer, or jump to one with <kbd>' +
              MOD +
              '</kbd>+<kbd>P</kbd>.'
            : 'Open a folder to start editing.'
        }</p>
        <p><button class="btn small" data-act="open">Open Folder…</button></p>
        <div class="recents"></div>
        <p class="muted">Terminal <kbd>${MOD}</kbd>+<kbd>\`</kbd> ·
           Search <kbd>${MOD}</kbd>+<kbd>Shift</kbd>+<kbd>F</kbd> ·
           Save <kbd>${MOD}</kbd>+<kbd>S</kbd></p>
      </div>`);
    wrap.querySelector('[data-act="open"]').addEventListener('click', pickFolder);
    const recents = wrap.querySelector('.recents');
    for (const path of S.recents.slice(0, 6)) {
      const row = el(
        `<button class="recent-row" title="${esc(path)}">
           <span class="ticon">${window.Icons.folder}</span>
           <span class="recent-name">${esc(base(path))}</span>
           <span class="recent-path">${esc(path)}</span>
         </button>`
      );
      row.addEventListener('click', () => openFolder(path));
      recents.appendChild(row);
    }
    host.appendChild(wrap);
  }

  // ======================================================================
  // boot
  // ======================================================================

  async function health() {
    try {
      await api('/api/health');
      $('status-backend').textContent = 'backend up';
      $('status-backend').className = 'item';
    } catch (err) {
      $('status-backend').textContent = `backend down — ${err.message}`;
      $('status-backend').className = 'item err';
    }
  }

  async function boot() {
    S.config = await window.descant.config();
    S.api = S.config.api;

    window.Term.init($('terminal-host'));
    window.Editor.init($('monaco-host'));
    applySettings(window.Settings.init(applySettings));

    loadRecents();
    renderTargets();
    paintIdleChips();
    renderActivityBar();
    renderWelcome();
    window.Resize.init({
      onLayout: () => {
        window.Editor.layout();
        window.Term.fitActive();
      },
    });
    await health();
    setInterval(health, 15000);

    let last = null;
    try {
      last = localStorage.getItem(LAST_KEY);
    } catch {
      /* storage disabled */
    }
    if (last || S.config.openDir) await openFolder(last || S.config.openDir);
    else showView('files');

    // Git state goes stale when something outside the app touches the repo —
    // the terminal two inches down, or an agent in the panel.
    setInterval(() => {
      if (S.folder && !document.hidden) refreshGit();
    }, 5000);

    $('btn-refresh').addEventListener('click', async () => {
      if (!S.folder) return pickFolder();
      await post('/api/search/reindex', { root: S.folder }).catch(() => {});
      await refreshGit();
      showView(S.activeView);
      toast('reloaded');
    });
    $('btn-run-file').innerHTML = window.Icons.run;
    $('btn-run-file').addEventListener('click', runOpenFile);
    $('btn-new-session').addEventListener('click', pickFolder);
    $('btn-send').addEventListener('click', sendChat);
    $('btn-run-terminal').addEventListener('click', runInTerminal);
    $('btn-stop').addEventListener('click', stopChat);
    $('btn-panel-close').addEventListener('click', () => togglePanel(false));
    $('chip-mode').addEventListener('click', showModeMenu);
    $('chip-model').addEventListener('click', showModelMenu);
    $('btn-chat-new').addEventListener('click', newChat);
    $('btn-chat-settings').addEventListener('click', showSettings);
    $('composer-target').addEventListener('change', (e) => {
      if (e.target.value === '__open__') {
        renderTargets();
        pickFolder();
        return;
      }
      openFolder(e.target.value);
    });
    // Enter sends, Shift+Enter is a newline — the terminal convention, not the
    // chat-app one. Cmd+Enter keeps working because the menu accelerator routes
    // it here when the composer has focus.
    const composer = $('composer-input');
    composer.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat();
        return;
      }
      if (e.key === 'Escape') {
        composer.blur();
      }
    });
    // Grow with the text, up to a point; a composer that swallows the log is
    // worse than one you have to scroll.
    const autoGrow = () => {
      composer.style.height = 'auto';
      composer.style.height = `${Math.min(composer.scrollHeight, 220)}px`;
    };
    composer.addEventListener('input', autoGrow);

    // A pending permission answers to a/A/d, as long as you are not typing.
    $('agent-panel').addEventListener('keydown', (e) => {
      if (e.target === composer || e.metaKey || e.ctrlKey || e.altKey) return;
      if (activeChat()?.key(e.key)) e.preventDefault();
    });
    $('status-branch').addEventListener('click', (e) => {
      if (S.git?.is_repo) showBranchMenu(e);
    });

    const handleMenu = (action) => {
      // A menu accelerator fires no matter what has focus, which is the whole
      // reason it works inside Monaco — but it also means Cmd+Enter would run a
      // file while you are mid-sentence in the composer. In the composer it
      // keeps meaning "send".
      if (action === 'run-file') {
        if (document.activeElement === $('composer-input')) sendChat();
        else runOpenFile();
      } else if (action === 'save-file') saveOpenFile();
      else if (action === 'open-folder') pickFolder();
      else if (action === 'new-file') S.folder && createEntry(S.folder, false);
      else if (action === 'toggle-terminal') togglePanel();
      else if (action === 'close-tab' && S.activeTab) closeTab(S.activeTab);
      else if (action === 'quick-open') toggleQuickOpen();
      else if (action === 'view-explorer') showView('files');
      else if (action === 'view-search') showView('search');
      else if (action === 'view-scm') showView('scm');
      else if (action === 'find') window.Editor.find();
    };
    window.descant.onMenu(handleMenu);
    // Exposed so the headless checks can exercise the same paths the UI uses.
    window.__menuHandler = handleMenu;
    window.__chatForTest = activeChat;

    // The editor gets its own menu; Monaco's built-in one is replaced so Save
    // and Run are where you expect them.
    window.ContextMenu.attach($('monaco-host'), () => {
      if (!S.openFilePath) return null;
      return [
        { label: 'Save', hint: `${MOD}+S`, disabled: !window.Editor.isDirty(), action: saveOpenFile },
        { label: 'Run File', hint: `${MOD}+↵`, action: runOpenFile },
        { separator: true },
        { label: 'Copy Path', action: () => navigator.clipboard.writeText(S.openFilePath) },
      ];
    });

    // In the terminal, the useful menu is the shell's clipboard, not ours.
    window.ContextMenu.attach($('terminal-host'), () => [
      {
        label: 'Paste',
        action: async () => {
          const text = await navigator.clipboard.readText();
          if (text) window.Term.send(S.folder, text.replace(/\n$/, ''));
        },
      },
      { label: 'Clear', action: () => window.Term.send(S.folder, 'clear') },
    ]);

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
