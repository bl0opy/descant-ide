// Settings.
//
// Only things that actually change behaviour appear here. A settings screen
// full of toggles that do nothing is worse than no settings screen, so every
// row below is wired to something real, and the ones Descant cannot change from
// inside the app (which CLI it spawns, which port it serves) are shown as facts
// with the environment variable that governs them.
window.Settings = (() => {
  const KEY = 'descant.settings';

  const DEFAULTS = {
    // editing
    autoSave: 'off',          // 'off' | 'delay' | 'blur'
    autoSaveDelay: 1000,      // ms of quiet before an autosave, when 'delay'
    tabSize: 4,
    wordWrap: false,
    // appearance
    fontSize: 12,             // editor and terminal
    minimap: true,
    lineNumbers: true,
    bracketPairs: true,
    // explorer
    showHidden: true,         // dotfiles in the explorer
    // chat
    permissionMode: 'manual', // what a new chat starts in
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

  function number(value, { min, max, step = 1, onSet }) {
    const input = el(
      `<input class="set-number" type="number" min="${min}" max="${max}" step="${step}" value="${value}" />`
    );
    input.addEventListener('change', () => {
      const n = Math.min(max, Math.max(min, parseInt(input.value, 10) || value));
      input.value = n;
      onSet(n);
    });
    return input;
  }

  function render(host, { config, folder, onResetLayout, onClearChats }) {
    host.innerHTML = '';
    const wrap = el('<div class="inspector"></div>');
    wrap.appendChild(
      el(
        `<div><h1>Settings</h1><div class="subtitle">Stored on this machine, applied immediately.</div></div>`
      )
    );

    // --- editing --------------------------------------------------------
    wrap.appendChild(el('<h2>Editing</h2>'));

    const auto = el(`
      <select class="set-select">
        <option value="off">Off — save with ${
          navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'
        }S</option>
        <option value="delay">After a pause in typing</option>
        <option value="blur">When the editor loses focus</option>
      </select>`);
    auto.value = current.autoSave;
    const delayRow = el(`
      <div class="set-row set-sub">
        <div class="set-label">
          <div class="set-name">Pause before saving</div>
          <div class="set-help">Milliseconds of quiet before an autosave fires.</div>
        </div>
        <div class="set-control"></div>
      </div>`);
    delayRow
      .querySelector('.set-control')
      .appendChild(
        number(current.autoSaveDelay, {
          min: 200,
          max: 10000,
          step: 100,
          onSet: (n) => set({ autoSaveDelay: n }),
        })
      );
    const syncDelayRow = () => {
      delayRow.style.display = auto.value === 'delay' ? '' : 'none';
    };
    auto.addEventListener('change', () => {
      set({ autoSave: auto.value });
      syncDelayRow();
    });
    wrap.appendChild(
      row(
        'Auto-save',
        'Off by default: a file that saves itself while an agent is reading it is a surprise, so this is opt-in.',
        auto
      )
    );
    wrap.appendChild(delayRow);
    syncDelayRow();

    wrap.appendChild(
      row('Tab size', 'Spaces per indent level.', number(current.tabSize, {
        min: 1,
        max: 8,
        onSet: (n) => set({ tabSize: n }),
      }))
    );
    wrap.appendChild(
      row(
        'Word wrap',
        'Wrap long lines at the viewport edge instead of scrolling sideways.',
        toggle(current.wordWrap, (v) => set({ wordWrap: v }))
      )
    );

    // --- appearance -----------------------------------------------------
    wrap.appendChild(el('<h2>Appearance</h2>'));

    wrap.appendChild(
      row(
        'Editor and terminal font size',
        'In pixels.',
        number(current.fontSize, { min: 9, max: 24, onSet: (n) => set({ fontSize: n }) })
      )
    );
    wrap.appendChild(
      row('Minimap', 'The document overview down the right edge.', toggle(current.minimap, (v) => set({ minimap: v })))
    );
    wrap.appendChild(
      row('Line numbers', '', toggle(current.lineNumbers, (v) => set({ lineNumbers: v })))
    );
    wrap.appendChild(
      row(
        'Bracket pair guides',
        'Coloured vertical guides linking matching brackets.',
        toggle(current.bracketPairs, (v) => set({ bracketPairs: v }))
      )
    );

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
        'Dotfiles like <code>.gitignore</code> and <code>.env</code>. Build directories (<code>node_modules</code>, <code>.git</code>, <code>dist</code>) are always hidden.',
        toggle(current.showHidden, (v) => set({ showHidden: v }))
      )
    );

    // --- chat -----------------------------------------------------------
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

    const clear = el('<button class="btn small secondary danger">Close all chats</button>');
    clear.addEventListener('click', onClearChats);
    wrap.appendChild(
      row(
        'Close every open conversation',
        'Stops the <code>claude</code> processes behind them. Files on disk are not touched.',
        clear
      )
    );

    // --- facts ----------------------------------------------------------
    wrap.appendChild(
      el(
        `<h2>Environment <span class="h2-note">set outside the app, shown here so you know what is in effect</span></h2>`
      )
    );
    const facts = [
      ['Open folder', folder || 'none', ''],
      ['Backend', config.api, 'DESCANT_PORT'],
      [
        'Terminal',
        config.ptyAvailable ? 'available' : `unavailable — ${config.ptyError || 'unknown'}`,
        'SHELL',
      ],
      ['Claude CLI', config.claude || 'not found on PATH', 'DESCANT_CLAUDE_BIN'],
      ['ripgrep', config.ripgrep || 'not installed — using the slower Python walk', ''],
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

    host.appendChild(wrap);
  }

  return { init, get, set, render, DEFAULTS };
})();
