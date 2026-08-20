// Inline SVG icons in the Codicon idiom (24x24, currentColor, stroke-based).
// Inlined rather than pulled from a font so the app has no network dependency
// and the CSP stays tight.
window.Icons = (() => {
  const wrap = (body, size = 24) =>
    `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;

  return {
    sessions: wrap(
      '<path d="M4 5h16M4 12h16M4 19h10"/><circle cx="19.5" cy="19" r="2" fill="currentColor" stroke="none"/>'
    ),
    files: wrap(
      '<path d="M13 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9z"/><path d="M13 3v6h6"/>'
    ),
    context: wrap(
      '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18"/><path d="M12 12l6-3"/>'
    ),
    terminal: wrap('<rect x="3" y="4" width="18" height="16" rx="1.5"/><path d="M7 9l3 3-3 3M13 15h4"/>'),
    chevronDown: wrap('<path d="M6 9l6 6 6-6"/>', 16),
    chevronRight: wrap('<path d="M9 6l6 6-6 6"/>', 16),
    close: wrap('<path d="M6 6l12 12M18 6L6 18"/>', 16),
    play: wrap('<path d="M7 4l12 8-12 8z"/>', 16),
    warn: wrap(
      '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.5v.5"/>',
      16
    ),
  };
})();
