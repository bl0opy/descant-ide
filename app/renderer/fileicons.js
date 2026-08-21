// Filetype icons, in the Catppuccin Mocha palette.
//
// The point of a coloured icon set is recognition before reading: you should
// find the Python file by its colour, not by scanning names. So the colour
// carries the language and the glyph carries the *category* (code, markup,
// config, data, image, doc) — which keeps the set small without making every
// file look alike.
window.FileIcons = (() => {
  // Catppuccin Mocha.
  const C = {
    rosewater: '#f5e0dc',
    flamingo: '#f2cdcd',
    pink: '#f5c2e7',
    mauve: '#cba6f7',
    red: '#f38ba8',
    maroon: '#eba0ac',
    peach: '#fab387',
    yellow: '#f9e2af',
    green: '#a6e3a1',
    teal: '#94e2d5',
    sky: '#89dceb',
    sapphire: '#74c7ec',
    blue: '#89b4fa',
    lavender: '#b4befe',
    text: '#cdd6f4',
    overlay: '#7f849c',
  };

  // Glyph bodies, drawn inside a 16x16 box.
  const G = {
    code: '<path d="M6 5.5 3 8l3 2.5M10 5.5 13 8l-3 2.5"/>',
    markup: '<path d="M5.5 4 4 8l1.5 4M10.5 4 12 8l-1.5 4M9.4 3.6 6.6 12.4"/>',
    config: '<circle cx="8" cy="8" r="2.1"/><path d="M8 2.6v1.6M8 11.8v1.6M2.6 8h1.6M11.8 8h1.6M4.2 4.2l1.1 1.1M10.7 10.7l1.1 1.1M11.8 4.2l-1.1 1.1M5.3 10.7l-1.1 1.1"/>',
    data: '<ellipse cx="8" cy="4.4" rx="4.6" ry="1.8"/><path d="M3.4 4.4v7.2c0 1 2.06 1.8 4.6 1.8s4.6-.8 4.6-1.8V4.4"/><path d="M3.4 8c0 1 2.06 1.8 4.6 1.8s4.6-.8 4.6-1.8"/>',
    doc: '<path d="M4 3.4h8M4 6.4h8M4 9.4h6M4 12.4h4"/>',
    image: '<rect x="2.6" y="3.4" width="10.8" height="9.2" rx="1.2"/><circle cx="6" cy="6.6" r="1.1"/><path d="M3 11.4 6.2 8.6l2.2 1.9 2.1-2.2 2.5 2.6"/>',
    shell: '<rect x="2.4" y="3.2" width="11.2" height="9.6" rx="1.2"/><path d="M5 6.6 7 8.4 5 10.2M8.6 10.4h2.6"/>',
    lock: '<rect x="3.6" y="7" width="8.8" height="6.2" rx="1.2"/><path d="M5.8 7V5.4a2.2 2.2 0 0 1 4.4 0V7"/>',
    file: '<path d="M4 2.4h4.6L12 5.8v7.8H4z"/><path d="M8.4 2.4v3.6H12"/>',
    folder: '<path d="M2 4.2h4l1.4 1.6H14v7.2H2z"/>',
    folderOpen: '<path d="M2 4.2h4l1.4 1.6H14v1.4H2z"/><path d="M2 7.2h12l-1.6 5.8H2z"/>',
  };

  // extension -> [colour, glyph]. Anything unlisted falls back to a plain file.
  const BY_EXT = {
    js: [C.yellow, G.code],
    mjs: [C.yellow, G.code],
    cjs: [C.yellow, G.code],
    jsx: [C.sky, G.code],
    ts: [C.blue, G.code],
    tsx: [C.sky, G.code],
    py: [C.yellow, G.code],
    rb: [C.red, G.code],
    go: [C.sapphire, G.code],
    rs: [C.peach, G.code],
    java: [C.red, G.code],
    c: [C.blue, G.code],
    h: [C.blue, G.code],
    cpp: [C.blue, G.code],
    hpp: [C.blue, G.code],
    cs: [C.green, G.code],
    php: [C.mauve, G.code],
    swift: [C.peach, G.code],
    kt: [C.mauve, G.code],
    dart: [C.sapphire, G.code],
    lua: [C.blue, G.code],
    r: [C.blue, G.code],
    ino: [C.teal, G.code],

    html: [C.peach, G.markup],
    htm: [C.peach, G.markup],
    xml: [C.peach, G.markup],
    svg: [C.pink, G.image],
    vue: [C.green, G.markup],
    css: [C.blue, G.markup],
    scss: [C.pink, G.markup],
    sass: [C.pink, G.markup],
    less: [C.blue, G.markup],

    json: [C.yellow, G.config],
    jsonl: [C.yellow, G.data],
    yaml: [C.mauve, G.config],
    yml: [C.mauve, G.config],
    toml: [C.peach, G.config],
    ini: [C.overlay, G.config],
    cfg: [C.overlay, G.config],
    conf: [C.overlay, G.config],
    env: [C.yellow, G.config],

    sql: [C.teal, G.data],
    db: [C.teal, G.data],
    sqlite: [C.teal, G.data],
    csv: [C.green, G.data],
    tsv: [C.green, G.data],

    md: [C.sky, G.doc],
    mdx: [C.sky, G.doc],
    txt: [C.text, G.doc],
    rst: [C.text, G.doc],
    pdf: [C.red, G.doc],

    sh: [C.green, G.shell],
    bash: [C.green, G.shell],
    zsh: [C.green, G.shell],
    fish: [C.green, G.shell],

    png: [C.pink, G.image],
    jpg: [C.pink, G.image],
    jpeg: [C.pink, G.image],
    gif: [C.pink, G.image],
    webp: [C.pink, G.image],
    ico: [C.pink, G.image],

    lock: [C.overlay, G.lock],
    zip: [C.maroon, G.lock],
    tar: [C.maroon, G.lock],
    gz: [C.maroon, G.lock],
  };

  // Whole filenames that mean something regardless of extension.
  const BY_NAME = {
    'package.json': [C.green, G.config],
    'package-lock.json': [C.overlay, G.lock],
    'tsconfig.json': [C.blue, G.config],
    dockerfile: [C.sapphire, G.config],
    makefile: [C.peach, G.config],
    '.gitignore': [C.maroon, G.config],
    '.gitattributes': [C.maroon, G.config],
    'readme.md': [C.sky, G.doc],
    license: [C.yellow, G.doc],
    'requirements.txt': [C.yellow, G.config],
    'pyproject.toml': [C.yellow, G.config],
    'claude.md': [C.peach, G.doc],
    'skill.md': [C.mauve, G.doc],
  };

  const svg = (body, color) =>
    `<svg class="ficon" viewBox="0 0 16 16" width="16" height="16" fill="none" ` +
    `stroke="${color}" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;

  function forFile(name) {
    const lower = (name || '').toLowerCase();
    const named = BY_NAME[lower];
    if (named) return svg(named[1], named[0]);
    const ext = lower.includes('.') ? lower.split('.').pop() : '';
    const hit = BY_EXT[ext];
    return hit ? svg(hit[1], hit[0]) : svg(G.file, C.overlay);
  }

  function forFolder(open) {
    return svg(open ? G.folderOpen : G.folder, C.blue);
  }

  return { forFile, forFolder, palette: C };
})();
