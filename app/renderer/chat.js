// The agent panel, as a conversation.
//
// The panel used to be a viewer: it tailed a transcript and you answered
// permission prompts somewhere else. This turns it into the place you actually
// talk to Claude Code — you type in the composer, the reply streams into the
// same log, and tool approvals are decided right there in the panel.
//
// This module owns no DOM of its own. It binds to the elements the panel
// already has, so the layout stays in index.html where the rest of the app's
// layout lives.
window.Chat = (() => {
  const { renderEvent } = window.Views;

  const STATUS_LABEL = {
    idle: 'ready',
    running: 'working…',
    needs_input: 'waiting on you',
    starting: 'starting…',
    failed: 'failed',
    closed: 'closed',
  };

  /**
   * Drive one conversation through the panel's elements.
   *
   * Returns a handle: the caller decides when a chat is shown, replaced, or
   * torn down, because the same panel also has to show read-only transcripts.
   */
  function attach({ chat, els, api, wsUrl, onDiff, onMeta }) {
    let meta = chat;
    let ws = null;
    let disposed = false;
    let openAssistant = null;
    let lastInitSession = null;
    // Our own copy, so the panel can leave a chat and come back to it.
    const history = [];

    const log = els.log;

    const atBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 100;
    const stick = (was) => {
      if (was) log.scrollTop = log.scrollHeight;
    };

    function setMeta(m) {
      if (!m || disposed) return;
      meta = m;
      onMeta?.(m);
    }

    function append(ev) {
      const was = atBottom();

      // Every turn re-inits the underlying process, and every approval restarts
      // it. Announcing "session started" each time is noise in a conversation —
      // say it once, and again only if the session itself really changed.
      if (ev.kind === 'status' && ev.subtype === 'init') {
        const sid = ev.meta?.session_id || null;
        if (lastInitSession !== null && sid === lastInitSession) return;
        lastInitSession = sid;
      }

      // Consecutive assistant chunks are one message; growing it in place keeps
      // the reply from stuttering into fragments.
      if (ev.kind === 'assistant_text' && openAssistant) {
        openAssistant.textContent += ev.text;
        stick(was);
        return;
      }
      if (ev.kind !== 'assistant_text' && ev.kind !== 'system') openAssistant = null;

      const node = renderEvent(ev, {
        container: log,
        onDiff,
        onPermission: async (pev, decision, remember) => {
          const res = await api(`/api/chats/${meta.chat_id}/permission`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ tool_use_id: pev.tool_use_id, decision, remember }),
          });
          setMeta(res.meta || res);
        },
      });
      if (!node) {
        stick(was);
        return;
      }
      if (ev.kind === 'assistant_text') openAssistant = node;
      if (ev.kind === 'user_text') node.classList.add('chat-mine');
      log.appendChild(node);
      stick(was);
    }

    function connect() {
      ws = new WebSocket(wsUrl(meta.chat_id));
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.kind === 'ping') return;
        if (msg.kind === 'meta') return setMeta(msg.chat);
        if (msg.kind === 'error') return record({ kind: 'error', text: msg.text, is_error: true });
        if (msg.event) {
          record(msg.event);
          setMeta(msg.meta);
        }
      };
      ws.onclose = () => {
        if (disposed) return;
        setTimeout(() => !disposed && connect(), 1200);
      };
    }

    async function send(text) {
      text = (text || '').trim();
      if (!text) return;
      try {
        setMeta(
          await api(`/api/chats/${meta.chat_id}/send`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ text }),
          })
        );
      } catch (err) {
        record({ kind: 'error', text: err.message, is_error: true });
        throw err;
      }
    }

    async function interrupt() {
      setMeta(await api(`/api/chats/${meta.chat_id}/interrupt`, { method: 'POST' }));
    }

    async function setMode(mode) {
      setMeta(
        await api(`/api/chats/${meta.chat_id}/mode`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ permission_mode: mode }),
        })
      );
    }

    /** Take an event in: remember it, then draw it. */
    function record(ev) {
      history.push(ev);
      if (history.length > 3000) history.shift();
      append(ev);
    }

    /** Redraw the whole conversation — used when the panel comes back to it.
     *  Draws from history without re-recording, so returning to a chat twice
     *  does not double its log. */
    function repaint() {
      log.innerHTML = '';
      openAssistant = null;
      lastInitSession = null;
      for (const ev of history) append(ev);
    }

    connect();

    return {
      get meta() {
        return meta;
      },
      send,
      interrupt,
      setMode,
      repaint,
      statusLabel: () => STATUS_LABEL[meta.status] || meta.status,
      dispose() {
        disposed = true;
        try {
          ws && ws.close();
        } catch {
          /* already gone */
        }
      },
    };
  }

  return { attach, STATUS_LABEL };
})();
