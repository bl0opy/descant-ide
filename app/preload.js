// Narrow bridge between the renderer and the main process.
// The renderer talks HTTP/WebSocket to Python for everything except terminals,
// which have to cross this boundary because node-pty is a native module.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('descant', {
  config: () => ipcRenderer.invoke('descant:config'),
  backendLog: () => ipcRenderer.invoke('descant:backendLog'),

  pty: {
    spawn: (opts) => ipcRenderer.invoke('pty:spawn', opts),
    write: (id, data) => ipcRenderer.send('pty:write', { id, data }),
    resize: (id, cols, rows) => ipcRenderer.send('pty:resize', { id, cols, rows }),
    kill: (id) => ipcRenderer.send('pty:kill', { id }),
    onData: (cb) => ipcRenderer.on('pty:data', (_e, payload) => cb(payload)),
    onExit: (cb) => ipcRenderer.on('pty:exit', (_e, payload) => cb(payload)),
  },

  onBackendDown: (cb) => ipcRenderer.on('backend:down', (_e, log) => cb(log)),
  onBackendExternal: (cb) => ipcRenderer.on('backend:external', (_e, api) => cb(api)),
});
