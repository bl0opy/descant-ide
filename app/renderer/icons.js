// Inline SVG icons in the Codicon idiom (24x24, currentColor, stroke-based).
// Inlined rather than pulled from a font so the app has no network dependency
// and the CSP stays tight.
window.Icons = (() => {
  const wrap = (body, size = 24) =>
    `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;

  return {
    files: wrap(
      '<path d="M13 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9z"/><path d="M13 3v6h6"/>'
    ),
    // Search: the magnifier, same glyph the results panel is named for.
    search: wrap('<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21"/>'),
    // Source control: a branch — two commits off a trunk.
    scm: wrap(
      '<circle cx="7" cy="6" r="2.2"/><circle cx="7" cy="18" r="2.2"/><circle cx="17" cy="9" r="2.2"/>' +
        '<path d="M7 8.2v7.6M17 11.2c0 3-2.4 4.4-6.4 4.8"/>'
    ),
    terminal: wrap('<rect x="3" y="4" width="18" height="16" rx="1.5"/><path d="M7 9l3 3-3 3M13 15h4"/>'),
    // Chat: a speech bubble, i.e. "talk to it here".
    chat: wrap(
      '<path d="M20 12a8 8 0 0 1-8 8H7l-3 2v-4.2A8 8 0 1 1 20 12z"/><path d="M8.5 11h7M8.5 14h4"/>'
    ),
    // Settings: a gear.
    settings: wrap(
      '<circle cx="12" cy="12" r="3.2"/>' +
        '<path d="M12 3.4v2.2M12 18.4v2.2M3.4 12h2.2M18.4 12h2.2' +
        'M6 6l1.6 1.6M16.4 16.4L18 18M18 6l-1.6 1.6M7.6 16.4L6 18"/>'
    ),

    // --- small glyphs, 16px ------------------------------------------------
    chevronDown: wrap('<path d="M6 9l6 6 6-6"/>', 16),
    chevronRight: wrap('<path d="M9 6l6 6-6 6"/>', 16),
    close: wrap('<path d="M6 6l12 12M18 6L6 18"/>', 16),
    play: wrap('<path d="M7 4l12 8-12 8z"/>', 16),
    check: wrap('<path d="M4 12l5 5L20 6"/>', 16),
    plus: wrap('<path d="M12 5v14M5 12h14"/>', 16),
    minus: wrap('<path d="M5 12h14"/>', 16),
    // Discard: an undo arc, because what it does is put the file back.
    discard: wrap('<path d="M4 9h9a5 5 0 0 1 0 10H8"/><path d="M8 5L4 9l4 4"/>', 16),
    // Sync: the two-arrow ring every git client uses for pull-then-push.
    sync: wrap(
      '<path d="M4 11a8 8 0 0 1 13.3-5.9L20 7"/><path d="M20 3v4h-4"/>' +
        '<path d="M20 13a8 8 0 0 1-13.3 5.9L4 17"/><path d="M4 21v-4h4"/>',
      16
    ),
    folder: wrap('<path d="M3 6h6l2 2.5h10V19H3z"/>', 16),
    warn: wrap('<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.5v.5"/>', 16),
    // An outlined play arrow, for "run this". Outlined rather than solid so it
    // reads as an affordance next to the wordmark instead of a status light.
    run: wrap('<path d="M8 5.2l10 6.8-10 6.8z" stroke-width="1.8"/>', 20),
  };
})();
