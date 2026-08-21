// Draggable splitters.
//
// The whole layout is driven by three CSS variables, so resizing is just
// writing to them — no reflow logic, no per-pane bookkeeping. Sizes persist
// across launches, because a pane you widened once you almost certainly want
// wide next time too.
window.Resize = (() => {
  const KEY = 'descant.layout';

  // variable -> [min, max] in px. Maxima are computed against the window so a
  // small display cannot end up with a center pane of zero width.
  const LIMITS = {
    '--sidebar-width': () => [180, Math.max(220, window.innerWidth * 0.5)],
    '--agent-panel-width': () => [280, Math.max(320, window.innerWidth * 0.6)],
    '--panel-height': () => [100, Math.max(140, window.innerHeight * 0.75)],
  };

  const root = document.documentElement;
  const read = (name) => parseInt(getComputedStyle(root).getPropertyValue(name), 10) || 0;

  function save() {
    const out = {};
    for (const name of Object.keys(LIMITS)) out[name] = read(name);
    try {
      localStorage.setItem(KEY, JSON.stringify(out));
    } catch {
      /* storage disabled; sizes just won't persist */
    }
  }

  function restore() {
    let saved;
    try {
      saved = JSON.parse(localStorage.getItem(KEY) || '{}');
    } catch {
      return;
    }
    for (const [name, value] of Object.entries(saved)) {
      if (!LIMITS[name] || typeof value !== 'number') continue;
      const [min, max] = LIMITS[name]();
      root.style.setProperty(name, `${Math.min(max, Math.max(min, value))}px`);
    }
  }

  /**
   * @param opts.axis      'x' | 'y'
   * @param opts.invert    true when dragging right/down should *shrink* the pane
   *                       (the agent panel grows leftward, the terminal upward)
   * @param opts.onMove    called while dragging, for panes that must re-layout
   */
  function makeHandle(el, { variable, axis, invert = false, onMove }) {
    el.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      const startPos = axis === 'x' ? e.clientX : e.clientY;
      const startSize = read(variable);
      const [min, max] = LIMITS[variable]();
      el.setPointerCapture(e.pointerId);
      el.classList.add('dragging');
      document.body.classList.add('resizing');

      const move = (ev) => {
        const delta = (axis === 'x' ? ev.clientX : ev.clientY) - startPos;
        const next = startSize + (invert ? -delta : delta);
        root.style.setProperty(variable, `${Math.min(max, Math.max(min, next))}px`);
        onMove?.();
      };
      const up = () => {
        el.removeEventListener('pointermove', move);
        el.removeEventListener('pointerup', up);
        el.classList.remove('dragging');
        document.body.classList.remove('resizing');
        save();
        onMove?.();
      };
      el.addEventListener('pointermove', move);
      el.addEventListener('pointerup', up);
    });

    // Double-click resets to the stylesheet's default.
    el.addEventListener('dblclick', () => {
      root.style.removeProperty(variable);
      save();
      onMove?.();
    });
  }

  function init({ onLayout } = {}) {
    restore();

    const body = document.getElementById('body');
    const sidebar = document.createElement('div');
    sidebar.className = 'splitter splitter-x';
    sidebar.id = 'splitter-sidebar';
    sidebar.title = 'Drag to resize · double-click to reset';

    const agent = document.createElement('div');
    agent.className = 'splitter splitter-x';
    agent.id = 'splitter-agent';
    agent.title = 'Drag to resize · double-click to reset';

    body.appendChild(sidebar);
    body.appendChild(agent);

    makeHandle(sidebar, { variable: '--sidebar-width', axis: 'x', onMove: onLayout });
    makeHandle(agent, { variable: '--agent-panel-width', axis: 'x', invert: true, onMove: onLayout });

    // The terminal splitter lives above the panel, inside the center column.
    const panel = document.getElementById('panel');
    const term = document.createElement('div');
    term.className = 'splitter splitter-y';
    term.id = 'splitter-panel';
    term.title = 'Drag to resize · double-click to reset';
    panel.parentNode.insertBefore(term, panel);
    makeHandle(term, { variable: '--panel-height', axis: 'y', invert: true, onMove: onLayout });

    window.addEventListener('resize', () => {
      // Keep the saved sizes inside the new window's limits.
      for (const [name, limits] of Object.entries(LIMITS)) {
        const [min, max] = limits();
        const now = read(name);
        if (now < min || now > max) {
          root.style.setProperty(name, `${Math.min(max, Math.max(min, now))}px`);
        }
      }
      onLayout?.();
    });
  }

  return { init };
})();
