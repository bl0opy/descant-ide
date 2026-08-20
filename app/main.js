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

const { app, BrowserWindow, ipcMain, shell } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const http = require('node:http');
const fs = require('node:fs');

const ROOT = path.resolve(__dirname, '..');
const BACKEND_DIR = path.join(ROOT, 'backend');

const HOST = process.env.DESCANT_HOST || '127.0.0.1';
const PORT = parseInt(process.env.DESCANT_PORT || '8787', 10);
const API = `http://${HOST}:${PORT}`;

// Default to the bundled fixtures so a fresh clone shows something real on
// first launch.  Point DESCANT_PROJECTS_DIR at ~/.claude/projects for real data.
const PROJECTS_DIR =
  process.env.DESCANT_PROJECTS_DIR || path.join(ROOT, 'fixtures', 'projects');

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
    return { ok: false, error: String(err.message || err) };
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
