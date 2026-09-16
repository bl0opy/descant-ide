// Right-click / two-finger-tap menus.
//
// Built in the renderer rather than through Electron's native Menu because
// every item here acts on renderer state (this tree row, this session, this
// buffer) — routing that through IPC would buy nothing but latency. Styling
// follows the rest of the app rather than the OS, which is the trade every
// editor makes for these.
window.ContextMenu = (() => {
  let open = null;

  function close() {
    if (!open) return;
    open.remove();
    open = null;
    document.removeEventListener('pointerdown', onAway, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('blur', close);
  }

  const onAway = (e) => {
    if (open && !open.contains(e.target)) close();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') close();
  };

  /**
   * @param items  [{label, action, danger, disabled} | {separator: true}]
   */
  function show(x, y, items) {
    close();
    const menu = document.createElement('div');
    menu.className = 'ctx-menu';

    for (const item of items) {
      if (!item) continue;
      if (item.separator) {
        menu.appendChild(Object.assign(document.createElement('div'), { className: 'ctx-sep' }));
        continue;
      }
      const row = document.createElement('button');
      row.className = 'ctx-item' + (item.danger ? ' danger' : '');
      row.disabled = Boolean(item.disabled);
      // Labels can carry repo-controlled text (a branch name, a filename), so
      // they are escaped rather than trusted into innerHTML.
      const esc = (s) =>
        String(s == null ? '' : s).replace(
          /[&<>"']/g,
          (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
        );
      row.innerHTML = `<span>${esc(item.label)}</span>${
        item.hint ? `<span class="ctx-hint">${esc(item.hint)}</span>` : ''
      }`;
      row.addEventListener('click', () => {
        close();
        item.action?.();
      });
      menu.appendChild(row);
    }

    // Off-screen at the pointer is worse than useless, so measure and flip.
    menu.style.visibility = 'hidden';
    document.body.appendChild(menu);
    const { width, height } = menu.getBoundingClientRect();
    menu.style.left = `${Math.min(x, window.innerWidth - width - 8)}px`;
    menu.style.top = `${Math.min(y, window.innerHeight - height - 8)}px`;
    menu.style.visibility = '';

    open = menu;
    document.addEventListener('pointerdown', onAway, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('blur', close);
  }

  /** Attach a menu to an element; `build` returns the items for that event. */
  function attach(el, build) {
    el.addEventListener('contextmenu', (e) => {
      const items = build(e);
      if (!items || !items.length) return;
      e.preventDefault();
      e.stopPropagation();
      show(e.clientX, e.clientY, items);
    });
  }

  return { show, attach, close };
})();
