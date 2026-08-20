// Terminal panel: xterm.js in the renderer, node-pty over IPC in main.
// One pty per repo path, so switching sessions between repos gives you the
// right shell rather than one global terminal.
window.Term = (() => {
  // xterm and addon-fit ship UMD bundles loaded by <script> tags in index.html,
  // which land on window.  They must load *before* Monaco's AMD loader defines
  // `define.amd`, or they register as anonymous AMD modules and never appear
  // here.  index.html keeps that order.
  // xterm spreads its exports onto globalThis, so `Terminal` is the class.
  // addon-fit assigns its whole module namespace, so the class is one level in.
  const Terminal = window.Terminal || null;
  const FitAddon = window.FitAddon?.FitAddon || window.FitAddon || null;
  const loadError = Terminal
    ? FitAddon
      ? null
      : 'addon-fit did not load'
    : 'xterm.js did not load';

  const sessions = new Map(); // id -> {term, fit, el}
  let host = null;
  let activeId = null;
  let wired = false;

  // Colours are read out of theme.css so the terminal restyles with everything
  // else rather than carrying its own hardcoded palette.
  function themeFromCss() {
    const s = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (s.getPropertyValue(name) || fallback).trim();
    return {
      background: v('--bg-panel', '#181818'),
      foreground: v('--fg', '#cccccc'),
      cursor: v('--accent', '#3b82f6'),
      selectionBackground: v('--bg-selected', '#04395e'),
      black: v('--bg-app', '#1f1f1f'),
      red: v('--danger', '#f87171'),
      green: v('--success', '#4ade80'),
      yellow: v('--warning', '#fbbf24'),
      blue: v('--accent', '#3b82f6'),
      magenta: v('--cost-system', '#a78bfa'),
      cyan: v('--cost-conversation', '#38bdf8'),
      white: v('--fg-strong', '#e7e7e7'),
    };
  }

  function init(hostEl) {
    host = hostEl;
    if (!Terminal) {
      host.innerHTML = `<div id="terminal-fallback">xterm.js failed to load:\n${loadError}</div>`;
      return false;
    }
    if (!window.descant?.pty) return false;
    if (!wired) {
      window.descant.pty.onData(({ id, data }) => sessions.get(id)?.term.write(data));
      window.descant.pty.onExit(({ id, exitCode }) => {
        sessions.get(id)?.term.write(`\r\n\x1b[2m[process exited: ${exitCode}]\x1b[0m\r\n`);
      });
      window.addEventListener('resize', () => fitActive());
      wired = true;
    }
    return true;
  }

  async function open(cwd) {
    if (!Terminal || !host) return { ok: false, error: loadError || 'terminal unavailable' };
    const id = cwd || 'default';

    for (const [sid, s] of sessions) s.el.style.display = sid === id ? 'block' : 'none';
    activeId = id;

    if (sessions.has(id)) {
      fitActive();
      sessions.get(id).term.focus();
      return { ok: true, reused: true };
    }

    const el = document.createElement('div');
    el.style.width = '100%';
    el.style.height = '100%';
    host.appendChild(el);

    const term = new Terminal({
      fontFamily: getComputedStyle(document.documentElement)
        .getPropertyValue('--font-mono')
        .trim(),
      fontSize: 12,
      cursorBlink: true,
      allowProposedApi: true,
      theme: themeFromCss(),
      scrollback: 5000,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(el);
    sessions.set(id, { term, fit, el });

    try {
      fit.fit();
    } catch {
      /* host not laid out yet */
    }

    const res = await window.descant.pty.spawn({
      id,
      cwd,
      cols: term.cols,
      rows: term.rows,
    });
    if (!res.ok) {
      term.write(`\x1b[31mcould not start a shell: ${res.error}\x1b[0m\r\n`);
      return res;
    }
    term.onData((d) => window.descant.pty.write(id, d));
    term.onResize(({ cols, rows }) => window.descant.pty.resize(id, cols, rows));
    term.focus();
    return { ok: true };
  }

  function fitActive() {
    const s = sessions.get(activeId);
    if (!s) return;
    try {
      s.fit.fit();
      window.descant.pty.resize(activeId, s.term.cols, s.term.rows);
    } catch {
      /* not visible */
    }
  }

  function available() {
    return Boolean(Terminal);
  }

  /** Type a command into a repo's shell and press enter. */
  function send(cwd, command) {
    const id = cwd || 'default';
    if (!sessions.has(id)) return false;
    window.descant.pty.write(id, command.endsWith('\n') ? command : command + '\n');
    sessions.get(id).term.focus();
    return true;
  }

  function focus() {
    sessions.get(activeId)?.term.focus();
  }

  return { init, open, send, focus, fitActive, available, loadError: () => loadError };
})();
