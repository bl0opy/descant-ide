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

  const EXT_LANG = {
    js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
    ts: 'typescript', tsx: 'typescript', py: 'python', rb: 'ruby', go: 'go',
    rs: 'rust', java: 'java', c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp',
    cs: 'csharp', php: 'php', sh: 'shell', bash: 'shell', zsh: 'shell',
    json: 'json', jsonl: 'json', yml: 'yaml', yaml: 'yaml', toml: 'ini',
    ini: 'ini', md: 'markdown', html: 'html', css: 'css', scss: 'scss',
    sql: 'sql', xml: 'xml', dockerfile: 'dockerfile',
  };

  function langFor(path) {
    const name = (path || '').split('/').pop().toLowerCase();
    if (name === 'dockerfile') return 'dockerfile';
    return EXT_LANG[name.split('.').pop()] || 'plaintext';
  }

  function init(hostEl) {
    host = hostEl;
  }

  function disposeAll() {
    if (editor) { editor.dispose(); editor = null; }
    if (diffEditor) { diffEditor.dispose(); diffEditor = null; }
    if (host) host.innerHTML = '';
  }

  async function openFile(path, text) {
    await load();
    disposeAll();
    currentPath = path;
    editor = monaco.editor.create(host, {
      value: text,
      language: langFor(path),
      theme: 'descant',
      readOnly: false,
      automaticLayout: true,
      minimap: { enabled: true },
      fontSize: 12,
      fontFamily: getComputedStyle(document.documentElement)
        .getPropertyValue('--font-mono')
        .trim(),
      scrollBeyondLastLine: false,
      renderWhitespace: 'selection',
    });
    return editor;
  }

  /** Side-by-side diff — used to show what an agent edit changed. */
  async function openDiff(path, before, after) {
    await load();
    disposeAll();
    // Namespaced so a file tab and a diff tab for the same path are distinct.
    currentPath = `diff:${path}`;
    const lang = langFor(path);
    diffEditor = monaco.editor.createDiffEditor(host, {
      theme: 'descant',
      automaticLayout: true,
      readOnly: true,
      renderSideBySide: true,
      fontSize: 12,
      fontFamily: getComputedStyle(document.documentElement)
        .getPropertyValue('--font-mono')
        .trim(),
    });
    diffEditor.setModel({
      original: monaco.editor.createModel(before, lang),
      modified: monaco.editor.createModel(after, lang),
    });
    return diffEditor;
  }

  function layout() {
    editor?.layout();
    diffEditor?.layout();
  }

  return { init, load, openFile, openDiff, layout, current: () => currentPath };
})();
