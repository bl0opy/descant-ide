// Monaco editor + diff view.
//
// Monaco is loaded through its own AMD loader (already script-tagged in
// index.html) rather than bundled, so there is no build step in this project.
window.Editor = (() => {
  let monaco = null;
  let loading = null;
  let editor = null;
  let diffEditor = null;
  let host = null;
  let currentPath = null;

  //: Editor options the settings screen owns. Kept in one object so a new
  //: setting reaches a live editor, a new editor and the diff view by one path.
  let options = {
    fontSize: 12,
    tabSize: 4,
    wordWrap: 'off',
    minimap: true,
    lineNumbers: 'on',
    renderWhitespace: 'selection',
    bracketPairs: true,
    cursorBlinking: 'blink',
  };

  function monacoOptions() {
    return {
      fontSize: options.fontSize,
      tabSize: options.tabSize,
      wordWrap: options.wordWrap,
      minimap: { enabled: options.minimap },
      lineNumbers: options.lineNumbers,
      renderWhitespace: options.renderWhitespace,
      cursorBlinking: options.cursorBlinking,
      guides: { bracketPairs: options.bracketPairs, indentation: true },
      fontFamily: getComputedStyle(document.documentElement)
        .getPropertyValue('--font-mono')
        .trim(),
    };
  }

  function load() {
    if (monaco) return Promise.resolve(monaco);
    if (loading) return loading;
    loading = new Promise((resolve, reject) => {
      if (typeof window.require !== 'function' || !window.require.config) {
        reject(new Error('monaco loader unavailable'));
        return;
      }
      window.require.config({ paths: { vs: '../node_modules/monaco-editor/min/vs' } });
      // Monaco would otherwise fetch its workers cross-origin, which the CSP
      // blocks.  A blob shim keeps everything same-origin; syntax highlighting
      // and basic editing work fine, language servers are simply not started.
      window.MonacoEnvironment = {
        getWorkerUrl() {
          return URL.createObjectURL(
            new Blob(
              [
                `self.MonacoEnvironment={baseUrl:'${location.href.replace(
                  /\/[^/]*$/,
                  ''
                )}/../node_modules/monaco-editor/min/'};importScripts('${location.href.replace(
                  /\/[^/]*$/,
                  ''
                )}/../node_modules/monaco-editor/min/vs/base/worker/workerMain.js');`,
              ],
              { type: 'text/javascript' }
            )
          );
        },
      };
      window.require(['vs/editor/editor.main'], () => {
        monaco = window.monaco;
        defineTheme();
        resolve(monaco);
      }, reject);
    });
    return loading;
  }

  // Theme is derived from theme.css so the editor restyles with the app.
  function defineTheme() {
    const s = getComputedStyle(document.documentElement);
    const v = (n, f) => (s.getPropertyValue(n) || f).trim().replace(/^#?/, '#');
    monaco.editor.defineTheme('descant', {
      base: 'vs-dark',
      inherit: true,
      colors: {
        'editor.background': v('--bg-app', '1f1f1f'),
        'editor.foreground': v('--fg', 'cccccc'),
        'editorLineNumber.foreground': v('--fg-faint', '6e6e6e'),
        'editorLineNumber.activeForeground': v('--fg', 'cccccc'),
        'editor.selectionBackground': v('--bg-selected', '04395e'),
        'editorCursor.foreground': v('--accent', '3b82f6'),
        'editorIndentGuide.background1': v('--border', '2b2b2b'),
        'editorGutter.background': v('--bg-app', '1f1f1f'),
      },
      rules: [],
    });
  }

  // Language detection is delegated to Monaco's own registry rather than a
  // hand-written extension map: monaco-editor ships ~90 language definitions
  // and each one declares its extensions, filenames and aliases. Building the
  // index from that means every language Monaco supports works, and a Monaco
  // upgrade adds new ones for free.
  let langIndex = null;

  function buildLangIndex() {
    const byExt = new Map();
    const byFilename = new Map();
    for (const lang of monaco.languages.getLanguages()) {
      for (const ext of lang.extensions || []) {
        byExt.set(ext.toLowerCase(), lang.id); // ".py"
      }
      for (const name of lang.filenames || []) {
        byFilename.set(name.toLowerCase(), lang.id); // "Dockerfile"
      }
      for (const pattern of lang.filenamePatterns || []) {
        const m = /^\*?(\.[A-Za-z0-9._-]+)$/.exec(pattern);
        if (m) byExt.set(m[1].toLowerCase(), lang.id);
      }
    }
    // A few Claude Code touches constantly that Monaco doesn't claim by default.
    for (const [ext, id] of [
      ['.jsonl', 'json'],
      ['.mjs', 'javascript'],
      ['.cjs', 'javascript'],
      ['.zsh', 'shell'],
      ['.tf', 'hcl'],
      ['.toml', 'ini'],
      ['.lock', 'ini'],
    ]) {
      if (!byExt.has(ext) && monaco.languages.getLanguages().some((l) => l.id === id)) {
        byExt.set(ext, id);
      }
    }
    return { byExt, byFilename };
  }

  function langFor(path) {
    if (!monaco) return 'plaintext';
    if (!langIndex) langIndex = buildLangIndex();
    const name = (path || '').split('/').pop().toLowerCase();
    if (langIndex.byFilename.has(name)) return langIndex.byFilename.get(name);
    // Longest extension first so ".d.ts" beats ".ts".
    const dot = name.indexOf('.');
    for (let i = dot; i >= 0; i = name.indexOf('.', i + 1)) {
      const hit = langIndex.byExt.get(name.slice(i));
      if (hit) return hit;
    }
    return 'plaintext';
  }

  /** Every language this build can highlight — surfaced in the status bar. */
  function languageCount() {
    return monaco ? monaco.languages.getLanguages().length : 0;
  }

  // One Monaco model per file, kept for the life of the tab.
  //
  // This is load-bearing, not an optimisation. The editor used to be rebuilt
  // from a text snapshot every time you switched tabs, which meant switching
  // away and back silently reverted the buffer to whatever it said when it was
  // first opened — and with autosave on, that stale text then got written to
  // disk. Holding the live model means the buffer, its undo history and its
  // cursor survive a tab switch, and "what is on screen" is always the same
  // object the save reads from.
  const files = new Map(); // path -> {model, savedText, viewState}
  let handlers = {};
  let wired = false;

  function init(hostEl) {
    host = hostEl;
  }

  function disposeEditors() {
    if (editor) {
      editor.dispose();
      editor = null;
      wired = false;
    }
    if (diffEditor) {
      diffEditor.dispose();
      diffEditor = null;
    }
    if (host) host.innerHTML = '';
  }

  /** Kept for callers that mean "tear the view down"; models are untouched. */
  function disposeAll() {
    stashViewState();
    disposeEditors();
  }

  function stashViewState() {
    if (!editor || !currentPath) return;
    const entry = files.get(currentPath);
    if (entry) entry.viewState = editor.saveViewState();
  }

  async function openFile(path, text, nextHandlers) {
    await load();
    // Handlers come from whoever opened the file; a tab switch re-opens without
    // them, and dropping them would quietly disable dirty tracking and autosave.
    if (nextHandlers) handlers = nextHandlers;

    stashViewState();
    if (diffEditor) {
      diffEditor.dispose();
      diffEditor = null;
      if (host) host.innerHTML = '';
    }

    let entry = files.get(path);
    if (!entry) {
      // No content and nothing loaded yet: this is a tab activation racing the
      // read that will supply the text. Creating a model from `undefined` here
      // would open the file empty — and an empty buffer that autosaves is a
      // deleted file.
      if (text == null) return editor;
      entry = {
        model: monaco.editor.createModel(text, langFor(path)),
        savedText: text,
        viewState: null,
      };
      files.set(path, entry);
    }
    currentPath = path;

    const readOnly = Boolean(handlers.readOnly);
    if (!editor) {
      editor = monaco.editor.create(host, {
        model: entry.model,
        theme: 'descant',
        readOnly,
        automaticLayout: true,
        scrollBeyondLastLine: false,
        smoothScrolling: true,
        ...monacoOptions(),
      });
    } else {
      editor.setModel(entry.model);
      editor.updateOptions({ readOnly });
    }

    if (!wired) {
      // Bound to the editor, not the model, so they survive model swaps.
      editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => handlers.onRun?.());
      editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => handlers.onSave?.());
      editor.onDidChangeModelContent(() => handlers.onDirty?.(isDirty()));
      editor.onDidBlurEditorText(() => handlers.onBlur?.());
      wired = true;
    }

    if (entry.viewState) editor.restoreViewState(entry.viewState);
    return editor;
  }

  function isDirty() {
    const entry = files.get(currentPath);
    return Boolean(entry) && entry.model.getValue() !== entry.savedText;
  }

  /** The buffer as it stands, for whoever is doing the writing. */
  function currentText() {
    const entry = files.get(currentPath);
    return entry ? entry.model.getValue() : null;
  }

  /** Called after a successful write, so the dirty check has a new baseline. */
  function markSaved(text, path = currentPath) {
    const entry = files.get(path);
    if (entry) entry.savedText = text;
  }

  /** Forget a file entirely — only when its tab closes. */
  function closeFile(path) {
    const entry = files.get(path);
    if (!entry) return;
    if (currentPath === path && editor) {
      editor.setModel(null);
      currentPath = null;
    }
    entry.model.dispose();
    files.delete(path);
  }

  /** Whether *path* has unsaved edits, even if it is not the visible tab. */
  function isPathDirty(path) {
    const entry = files.get(path);
    return Boolean(entry) && entry.model.getValue() !== entry.savedText;
  }

  function textFor(path) {
    const entry = files.get(path);
    return entry ? entry.model.getValue() : null;
  }

  /** Side-by-side diff — used to show what an agent edit changed. */
  async function openDiff(path, before, after, key = null) {
    await load();
    stashViewState();
    disposeEditors();
    // Namespaced so a file tab and a diff tab for the same path are distinct,
    // and so two diffs of the same file (staged vs working) do not collide.
    currentPath = `diff:${key || path}`;
    const lang = langFor(path);
    diffEditor = monaco.editor.createDiffEditor(host, {
      theme: 'descant',
      automaticLayout: true,
      readOnly: true,
      renderSideBySide: true,
      ...monacoOptions(),
    });
    diffEditor.setModel({
      original: monaco.editor.createModel(before, lang),
      modified: monaco.editor.createModel(after, lang),
    });
    return diffEditor;
  }

  /** Settings have to reach a live editor, not just the next one created. */
  function applySettings(cfg) {
    options = {
      ...options,
      fontSize: cfg.fontSize ?? options.fontSize,
      tabSize: cfg.tabSize ?? options.tabSize,
      wordWrap: cfg.wordWrap ? 'on' : 'off',
      minimap: cfg.minimap ?? options.minimap,
      lineNumbers: cfg.lineNumbers === false ? 'off' : 'on',
      renderWhitespace: cfg.renderWhitespace || options.renderWhitespace,
      bracketPairs: cfg.bracketPairs ?? options.bracketPairs,
    };
    editor?.updateOptions(monacoOptions());
    diffEditor?.updateOptions(monacoOptions());
  }

  /** Scroll to a line and put the cursor on it — search results land here. */
  function reveal(line, column = 1) {
    if (!editor || !line) return;
    editor.revealLineInCenter(line);
    editor.setPosition({ lineNumber: line, column });
    editor.focus();
  }

  /** Monaco's own find widget, opened from our menu rather than its keybinding. */
  function find() {
    editor?.getAction('actions.find')?.run();
  }

  /** Where the cursor is, for the status bar. */
  function cursor() {
    const pos = editor?.getPosition();
    return pos ? { line: pos.lineNumber, column: pos.column } : null;
  }

  function layout() {
    editor?.layout();
    diffEditor?.layout();
  }

  return {
    init,
    load,
    openFile,
    openDiff,
    layout,
    applySettings,
    reveal,
    find,
    cursor,
    langFor,
    languageCount,
    current: () => currentPath,
    currentText,
    isDirty,
    markSaved,
    closeFile,
    isPathDirty,
    textFor,
  };
})();
