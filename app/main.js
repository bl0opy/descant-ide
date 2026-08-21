// Descant — Electron main process.
//
// Deliberately thin.  It does three things and nothing else:
//   1. supervise the Python backend (the actual brain of the app)
//   2. own the BrowserWindow
//   3. own node-pty, because a real terminal is the one thing the renderer
//      genuinely cannot do by itself
//
// Everything else — transcript parsing, context analysis, spawning `claude` —
// lives in Python and is reached over HTTP/WebSocket.

const { app, BrowserWindow, Menu, ipcMain, shell } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const http = require('node:http');
const fs = require('node:fs');

const ROOT = path.resolve(__dirname, '..');
const BACKEND_DIR = path.join(ROOT, 'backend');

const HOST = process.env.DESCANT_HOST || '127.0.0.1';
const PORT = parseInt(process.env.DESCANT_PORT || '8787', 10);
const API = `http://${HOST}:${PORT}`;

// Read the bundled fixtures *and* the real history, so a fresh clone shows
// something on first launch while live sessions — which run in the terminal and
// write to the real location — still show up. Set DESCANT_PROJECTS_DIR to
// override; it accepts a path.delimiter-separated list.
const REAL_PROJECTS_DIR = path.join(require('node:os').homedir(), '.claude', 'projects');
const PROJECTS_DIR =
  process.env.DESCANT_PROJECTS_DIR ||
  [path.join(ROOT, 'fixtures', 'projects'), REAL_PROJECTS_DIR].join(path.delimiter);

let backend = null;
let win = null;
const backendLog = [];

function logBackend(line) {
  backendLog.push(line);
  if (backendLog.length > 200) backendLog.shift();
  if (process.env.DESCANT_DEBUG) process.stdout.write(`[backend] ${line}\n`);
}

function pythonBin() {
  if (process.env.DESCANT_PYTHON) return process.env.DESCANT_PYTHON;
  const venv = path.join(ROOT, '.venv', 'bin', 'python3');
  return fs.existsSync(venv) ? venv : 'python3';
}

function startBackend() {
  if (process.env.DESCANT_NO_SPAWN) {
    logBackend('DESCANT_NO_SPAWN set — assuming a backend is already running');
    return;
  }
  backend = spawn(pythonBin(), ['-m', 'descant.server'], {
    cwd: BACKEND_DIR,
    env: {
      ...process.env,
      DESCANT_PROJECTS_DIR: PROJECTS_DIR,
      DESCANT_HOST: HOST,
      // Let the backend follow us down if we die without a clean shutdown;
      // chats own live `claude` processes that nothing else would stop.
      DESCANT_PARENT_PID: String(process.pid),
      DESCANT_PORT: String(PORT),
      PYTHONUNBUFFERED: '1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  backend.stdout.on('data', (d) => logBackend(d.toString().trimEnd()));
  backend.stderr.on('data', (d) => logBackend(d.toString().trimEnd()));
  backend.on('exit', async (code) => {
    logBackend(`exited with code ${code}`);
    backend = null;
    // A common case: the port was already taken by a backend someone started by
    // hand, so ours exited immediately.  The app is fine — it is talking to that
    // one — so say that rather than crying wolf.
    if (await ping()) {
      logBackend(`a backend is already serving ${API}; using it`);
      if (win && !win.isDestroyed()) win.webContents.send('backend:external', API);
      return;
    }
    if (win && !win.isDestroyed()) win.webContents.send('backend:down', backendLog.slice(-20));
  });
}

function ping() {
  return new Promise((resolve) => {
    const req = http.get(`${API}/api/health`, { timeout: 900 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForBackend(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await ping()) return true;
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

function createWindow() {
  win = new BrowserWindow({
    width: 1560,
    height: 960,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#1f1f1f', // matches --bg-app; avoids a white flash on boot
    title: 'Descant',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false, // node-pty is reached via IPC, but preload needs require()
    },
  });

  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.once('ready-to-show', () => win.show());

  // Dev aids.  DESCANT_DEBUG mirrors renderer console output into the terminal;
  // DESCANT_SCREENSHOT=<path> grabs the window and exits, which is how the UI
  // gets verified in a headless sandbox.
  if (process.env.DESCANT_DEBUG) {
    win.webContents.on('console-message', (_e, level, message, line, source) => {
      process.stdout.write(`[renderer:${level}] ${message} (${source}:${line})\n`);
    });
    win.webContents.on('render-process-gone', (_e, details) =>
      process.stdout.write(`[renderer] gone: ${JSON.stringify(details)}\n`)
    );
  }
  if (process.env.DESCANT_SCREENSHOT) {
    const delay = parseInt(process.env.DESCANT_SCREENSHOT_DELAY || '4000', 10);
    win.webContents.once('did-finish-load', () => {
      // Two ways to exercise a flow before the grab, so a headless check can
      // photograph more than the boot screen:
      //   DESCANT_AUTOCLICK=<css selector>   click one element
      //   DESCANT_DRIVE=<path to .js>        run an expression in the renderer
      const runIn = (js, tag, at) =>
        setTimeout(() => {
          win.webContents
            .executeJavaScript(js)
            .then((r) => process.stdout.write(`[${tag}] ${r}\n`))
            .catch((e) => process.stdout.write(`[${tag}] error: ${e}\n`));
        }, at);

      if (process.env.DESCANT_AUTOCLICK) {
        runIn(
          `(()=>{const e=document.querySelector(${JSON.stringify(
            process.env.DESCANT_AUTOCLICK
          )});if(e){e.click();return 'clicked'}return 'not found'})()`,
          'autoclick',
          Math.max(500, delay - 2500)
        );
      }
      if (process.env.DESCANT_DRIVE) {
        runIn(
          fs.readFileSync(process.env.DESCANT_DRIVE, 'utf8'),
          'drive',
          parseInt(process.env.DESCANT_DRIVE_DELAY || '3000', 10)
        );
      }
      setTimeout(async () => {
        try {
          const img = await win.webContents.capturePage();
          fs.writeFileSync(process.env.DESCANT_SCREENSHOT, img.toPNG());
          process.stdout.write(`[screenshot] wrote ${process.env.DESCANT_SCREENSHOT}\n`);
        } catch (err) {
          process.stdout.write(`[screenshot] failed: ${err}\n`);
        }
        app.quit();
      }, delay);
    });
  }
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}


// ---------------------------------------------------------------------------
// application menu
// ---------------------------------------------------------------------------
//
// The menu exists mainly for its accelerators. A shortcut bound in the renderer
// only fires when the renderer sees the keydown, and Monaco and xterm both eat
// keys before that — so Cmd+Enter would work on the welcome screen and nowhere
// useful. A menu accelerator fires regardless of what has focus.
function send(action) {
  return () => {
    if (process.env.DESCANT_DEBUG) process.stdout.write(`[menu] ${action}\n`);
    if (win && !win.isDestroyed()) win.webContents.send('menu:action', action);
  };
}

function buildMenu() {
  const isMac = process.platform === 'darwin';
  const template = [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: 'File',
      submenu: [
        { label: 'Save', accelerator: 'CmdOrCtrl+S', click: send('save-file') },
        { type: 'separator' },
        { label: 'New Session', accelerator: 'CmdOrCtrl+N', click: send('new-session') },
        { label: 'Close Tab', accelerator: 'CmdOrCtrl+W', click: send('close-tab') },
        ...(isMac ? [] : [{ type: 'separator' }, { role: 'quit' }]),
      ],
    },
    { role: 'editMenu' },
    {
      label: 'Run',
      submenu: [
        { label: 'Run File', accelerator: 'CmdOrCtrl+Return', click: send('run-file') },
        { label: 'Toggle Terminal', accelerator: 'CmdOrCtrl+`', click: send('toggle-terminal') },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---------------------------------------------------------------------------
// node-pty terminals
// ---------------------------------------------------------------------------

let pty = null;
let ptyLoadError = null;
try {
  pty = require('node-pty');
} catch (err) {
  ptyLoadError = String(err && err.message ? err.message : err);
}

const terminals = new Map();

// node-pty's failures are famously opaque: a spawn-helper without its executable
// bit reports nothing but "posix_spawnp failed.", which sends people hunting
// through shell config for a problem that is one chmod away.  Name it instead.
function explainSpawnFailure(err) {
  const raw = String((err && err.message) || err);
  if (!/posix_spawnp?\s+failed/i.test(raw)) return raw;
  const helper = path.join(
    __dirname,
    'node_modules',
    'node-pty',
    'prebuilds',
    `${process.platform}-${process.arch}`,
    'spawn-helper'
  );
  try {
    if (fs.existsSync(helper) && !(fs.statSync(helper).mode & 0o111)) {
      return `${raw} node-pty's spawn-helper is not executable — run \`npm run fix-pty\` (or chmod +x ${helper}).`;
    }
  } catch {
    /* fall through to the raw message */
  }
  return `${raw} (shell: ${process.env.SHELL || 'unset'})`;
}

ipcMain.handle('pty:spawn', (event, { id, cwd, cols, rows }) => {
  if (!pty) return { ok: false, error: `node-pty unavailable: ${ptyLoadError}` };
  if (terminals.has(id)) return { ok: true, reused: true };
  const shellPath =
    process.env.SHELL || (process.platform === 'win32' ? 'powershell.exe' : '/bin/bash');
  let proc;
  try {
    proc = pty.spawn(shellPath, [], {
      name: 'xterm-256color',
      cols: cols || 80,
      rows: rows || 24,
      cwd: cwd && fs.existsSync(cwd) ? cwd : process.env.HOME,
      env: { ...process.env, TERM: 'xterm-256color' },
    });
  } catch (err) {
    return { ok: false, error: explainSpawnFailure(err) };
  }
  proc.onData((data) => {
    if (win && !win.isDestroyed()) win.webContents.send('pty:data', { id, data });
  });
  proc.onExit(({ exitCode }) => {
    terminals.delete(id);
    if (win && !win.isDestroyed()) win.webContents.send('pty:exit', { id, exitCode });
  });
  terminals.set(id, proc);
  return { ok: true, shell: shellPath };
});

ipcMain.on('pty:write', (event, { id, data }) => {
  const p = terminals.get(id);
  if (p) p.write(data);
});

ipcMain.on('pty:resize', (event, { id, cols, rows }) => {
  const p = terminals.get(id);
  if (p && cols > 0 && rows > 0) {
    try {
      p.resize(cols, rows);
    } catch {
      /* pty already gone */
    }
  }
});

ipcMain.on('pty:kill', (event, { id }) => {
  const p = terminals.get(id);
  if (p) {
    try {
      p.kill();
    } catch {
      /* already dead */
    }
    terminals.delete(id);
  }
});

ipcMain.handle('descant:config', () => ({
  api: API,
  projectsDir: PROJECTS_DIR,
  ptyAvailable: Boolean(pty),
  ptyError: ptyLoadError,
  sandboxRepo: path.join(ROOT, 'sandbox-repo'),
  root: ROOT,
}));

ipcMain.handle('descant:backendLog', () => backendLog.slice(-60));

// ---------------------------------------------------------------------------

app.whenReady().then(async () => {
  startBackend();
  const up = await waitForBackend();
  if (!up) logBackend('backend did not answer /api/health in time');
  createWindow();
  buildMenu();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

function shutdown() {
  for (const [, p] of terminals) {
    try {
      p.kill();
    } catch {
      /* ignore */
    }
  }
  terminals.clear();
  if (backend) {
    backend.kill('SIGTERM');
    backend = null;
  }
}

app.on('window-all-closed', () => {
  shutdown();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', shutdown);
process.on('exit', shutdown);
