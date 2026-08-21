// Settings.
//
// Only things that actually change behaviour appear here. A settings screen
// full of toggles that do nothing is worse than no settings screen, so every
// row below is wired to something real, and the ones Descant cannot change from
// inside the app (where transcripts come from, which CLI it spawns) are shown
// as facts with the environment variable that governs them.
window.Settings = (() => {
  const KEY = 'descant.settings';

  const DEFAULTS = {
    permissionMode: 'manual', // what a new chat starts in
    fontSize: 12,             // editor and terminal
    showHidden: true,         // dotfiles in the explorer
    probeMcp: true,           // spawn MCP servers to measure them
  };

  let current = { ...DEFAULTS };
  let onChange = () => {};

  function load() {
    try {
      current = { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(KEY) || '{}') || {}) };
    } catch {
      current = { ...DEFAULTS };
    }
    return current;
  }

  function set(patch) {
    current = { ...current, ...patch };
    try {
      localStorage.setItem(KEY, JSON.stringify(current));
    } catch {
      /* storage disabled; settings just won't persist */
    }
    onChange(current);
  }

  const get = () => current;
  const init = (cb) => {
    onChange = cb || (() => {});
    return load();
  };

  const esc = (s) => window.Views.esc(s);
  const el = (html) => {
    const d = document.createElement('div');
    d.innerHTML = html.trim();
    return d.firstElementChild;
  };

  function row(label, help, control) {
    const r = el(`
      <div class="set-row">
        <div class="set-label">
          <div class="set-name">${esc(label)}</div>
          <div class="set-help">${help}</div>
        </div>
        <div class="set-control"></div>
      </div>`);
    r.querySelector('.set-control').appendChild(control);
    return r;
  }

  function toggle(value, onFlip) {
    const b = el(`<button class="toggle ${value ? 'on' : ''}"><span></span></button>`);
    b.addEventListener('click', () => {
      const next = !b.classList.contains('on');
      b.classList.toggle('on', next);
      onFlip(next);
    });
    return b;
  }

  function render(host, { config, onResetLayout, onClearChats }) {
    host.innerHTML = '';
    const wrap = el('<div class="inspector"></div>');
    wrap.appendChild(
      el(`<div><h1>Settings</h1><div class="subtitle">Stored on this machine, applied immediately.</div></div>`)
    );

    // --- behaviour ------------------------------------------------------
    wrap.appendChild(el('<h2>Chat</h2>'));

    const mode = el(`
      <select class="set-select">
        <option value="manual">Ask me — every tool needs a click</option>
        <option value="acceptEdits">Auto-accept edits — still asks for the rest</option>
        <option value="plan">Plan only — read and think, change nothing</option>
      </select>`);
    mode.value = current.permissionMode;
    mode.addEventListener('change', () => set({ permissionMode: mode.value }));
    wrap.appendChild(
      row(
        'Default permission mode',
        'What a <em>new</em> conversation starts in. Existing chats keep the mode you set in their header.',
        mode
      )
    );

    // --- appearance -----------------------------------------------------
    wrap.appendChild(el('<h2>Appearance</h2>'));

    const size = el(
      `<input class="set-number" type="number" min="9" max="24" step="1" value="${current.fontSize}" />`
    );
    size.addEventListener('change', () => {
      const n = Math.min(24, Math.max(9, parseInt(size.value, 10) || DEFAULTS.fontSize));
      size.value = n;
      set({ fontSize: n });
    });
    wrap.appendChild(row('Editor and terminal font size', 'In pixels.', size));

    const reset = el('<button class="btn small secondary">Reset pane sizes</button>');
    reset.addEventListener('click', onResetLayout);
    wrap.appendChild(
      row('Layout', 'Put the sidebar, chat panel and terminal back to their default widths.', reset)
    );

    // --- explorer -------------------------------------------------------
    wrap.appendChild(el('<h2>Explorer</h2>'));
    wrap.appendChild(
      row(
        'Show hidden files',
        'Dotfiles like <code>.mcp.json</code> and <code>.gitignore</code>. Build directories (<code>node_modules</code>, <code>.git</code>, <code>dist</code>) are always hidden.',
        toggle(current.showHidden, (v) => set({ showHidden: v }))
      )
    );

    // --- cost -----------------------------------------------------------
    wrap.appendChild(el('<h2>Measurement</h2>'));
    wrap.appendChild(
      row(
        'Probe MCP servers',
        'Descant measures a server by spawning it and asking what tools it exposes. That is the only way to get a real number, but it costs a process per server — turn it off for a faster, estimate-free loadout view.',
        toggle(current.probeMcp, (v) => set({ probeMcp: v }))
      )
    );

    // --- facts ----------------------------------------------------------
    wrap.appendChild(
      el(`<h2>Environment <span class="h2-note">set outside the app, shown here so you know what is in effect</span></h2>`)
    );
    const facts = [
      ['Transcripts', config.projectsDir, 'DESCANT_PROJECTS_DIR'],
      ['Backend', config.api, 'DESCANT_PORT'],
      ['Terminal', config.ptyAvailable ? 'available' : `unavailable — ${config.ptyError || 'unknown'}`, ''],
      ['Scratch repo', config.sandboxRepo, ''],
    ];
    for (const [name, value, envVar] of facts) {
      wrap.appendChild(
        el(`
        <div class="set-fact">
          <span class="set-fact-name">${esc(name)}</span>
          <code class="set-fact-value">${esc(value || '—')}</code>
          ${envVar ? `<span class="set-fact-env">${esc(envVar)}</span>` : ''}
        </div>`)
      );
    }

    // --- sessions -------------------------------------------------------
    wrap.appendChild(el('<h2>Live chats</h2>'));
    const clear = el('<button class="btn small secondary danger">Close all chats</button>');
    clear.addEventListener('click', onClearChats);
    wrap.appendChild(
      row(
        'Close every open conversation',
        'Stops the <code>claude</code> processes behind them. Transcripts on disk are not touched.',
        clear
      )
    );

    host.appendChild(wrap);
  }

  return { init, get, set, render, DEFAULTS };
})();
