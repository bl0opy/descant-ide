// The chat view: Claude Code as a conversation in the app.
//
// The terminal panel runs the real CLI and the agent panel replays transcripts;
// neither lets you *talk* to Claude from inside Descant. This does. It owns one
// live chat (a persistent `claude -p` on the backend), streams the reply as it
// arrives, and — the part that makes it a chat rather than a viewer — lets you
// answer permission requests in place.
window.Chat = (() => {
  const { esc, renderEvent } = window.Views;

  const el = (html) => {
    const d = document.createElement('div');
    d.innerHTML = html.trim();
    return d.firstElementChild;
  };

  const MODES = [
    ['manual', 'Ask me — every tool needs a click'],
    ['acceptEdits', 'Auto-accept edits — still asks for the rest'],
    ['plan', 'Plan only — read and think, change nothing'],
  ];

  const STATUS_LABEL = {
    starting: 'starting…',
    running: 'working…',
    idle: 'ready',
    needs_input: 'waiting on you',
    failed: 'failed',
    closed: 'closed',
  };

  /**
   * Mount a chat into *host*.
   *
   * `api` is the caller's fetch wrapper, so this file does no URL building and
   * no state juggling beyond the conversation itself.
   */
  function mount(host, { chat, api, wsUrl, onDiff }) {
    host.innerHTML = '';
    const view = el(`
      <div class="chat">
        <div class="chat-head">
          <div class="chat-title">
            <strong>${esc(chat.repo_name || chat.cwd)}</strong>
            <span class="chat-cwd">${esc(chat.cwd)}</span>
          </div>
          <div class="chat-head-right">
            <select class="chat-mode" title="How tool permissions are handled">
              ${MODES.map(
                ([v, label]) =>
                  `<option value="${v}" ${
                    v === chat.permission_mode ? 'selected' : ''
                  }>${esc(label)}</option>`
              ).join('')}
            </select>
            <span class="chat-status"></span>
          </div>
        </div>
        <div class="chat-log"></div>
        <div class="chat-composer">
          <textarea class="chat-input" rows="3"
                    placeholder="Ask Claude Code to do something in this repo…   (${
                      navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'
                    }+Enter to send)"></textarea>
          <div class="chat-composer-row">
            <span class="chat-cost"></span>
            <button class="btn secondary small chat-stop">Stop</button>
            <button class="btn small chat-send">Send</button>
          </div>
        </div>
      </div>`);
    host.appendChild(view);

    const log = view.querySelector('.chat-log');
    const input = view.querySelector('.chat-input');
    const statusEl = view.querySelector('.chat-status');
    const costEl = view.querySelector('.chat-cost');
    const sendBtn = view.querySelector('.chat-send');
    const stopBtn = view.querySelector('.chat-stop');
    const modeSel = view.querySelector('.chat-mode');

    let meta = chat;
    let ws = null;
    let closed = false;
    // Consecutive assistant chunks belong to one message; keeping the element
    // around lets the reply grow in place instead of stuttering into fragments.
    let openAssistant = null;

    const atBottom = () =>
      log.scrollHeight - log.scrollTop - log.clientHeight < 80;

    const stick = (wasAtBottom) => {
      if (wasAtBottom) log.scrollTop = log.scrollHeight;
    };

    function setMeta(m) {
      if (!m) return;
      meta = m;
      statusEl.textContent = STATUS_LABEL[m.status] || m.status;
      statusEl.className = 'chat-status s-' + m.status;
      costEl.textContent = m.cost_usd ? `$${m.cost_usd.toFixed(3)}` : '';
      const busy = m.status === 'running' || m.status === 'starting';
      sendBtn.disabled = busy;
      stopBtn.disabled = !busy;
      if (m.permission_mode && modeSel.value !== m.permission_mode) {
        modeSel.value = m.permission_mode;
      }
    }

    let lastInitSession = null;

    function append(ev) {
      const wasAtBottom = atBottom();

      // Every turn re-inits the underlying process, and every approval restarts
      // it. Announcing "session started" each time is noise in a conversation —
      // say it once, and only again if the session itself actually changed.
      if (ev.kind === 'status' && ev.subtype === 'init') {
        const sid = ev.meta?.session_id || null;
        if (lastInitSession !== null && sid === lastInitSession) return;
        lastInitSession = sid;
      }

      if (ev.kind === 'assistant_text') {
        // Grow the message in place.
        if (openAssistant) {
          openAssistant.textContent += ev.text;
          stick(wasAtBottom);
          return;
        }
      } else if (ev.kind !== 'system') {
        openAssistant = null;
      }

      const node = renderEvent(ev, {
        container: log,
        onDiff,
        onPermission: async (pev, decision, remember) => {
          const res = await api(`/api/chats/${meta.chat_id}/permission`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({
              tool_use_id: pev.tool_use_id,
              decision,
              remember,
            }),
          });
          setMeta(res.meta || res);
        },
      });
      if (!node) {
        stick(wasAtBottom);
        return;
      }
      if (ev.kind === 'assistant_text') openAssistant = node;
      if (ev.kind === 'user_text') node.classList.add('chat-mine');
      log.appendChild(node);
      stick(wasAtBottom);
    }

    function connect() {
      ws = new WebSocket(wsUrl(meta.chat_id));
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.kind === 'ping') return;
        if (msg.kind === 'meta') {
          setMeta(msg.chat);
          return;
        }
        if (msg.kind === 'error') {
          append({ kind: 'error', text: msg.text, is_error: true });
          return;
        }
        if (msg.event) {
          append(msg.event);
          setMeta(msg.meta);
        }
      };
      ws.onclose = () => {
        if (closed) return;
        // The backend restarts the process on every approval; the socket itself
        // survives that, so a close here means something else went wrong.
        setTimeout(() => !closed && connect(), 1200);
      };
    }

    async function send() {
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      sendBtn.disabled = true;
      try {
        setMeta(
          await api(`/api/chats/${meta.chat_id}/send`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ text }),
          })
        );
      } catch (err) {
        append({ kind: 'error', text: err.message, is_error: true });
        input.value = text;
        sendBtn.disabled = false;
      }
    }

    sendBtn.addEventListener('click', send);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        send();
      }
    });
    stopBtn.addEventListener('click', async () => {
      setMeta(await api(`/api/chats/${meta.chat_id}/interrupt`, { method: 'POST' }));
    });
    modeSel.addEventListener('change', async () => {
      setMeta(
        await api(`/api/chats/${meta.chat_id}/mode`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ permission_mode: modeSel.value }),
        })
      );
    });

    setMeta(chat);
    connect();
    setTimeout(() => input.focus(), 30);

    return {
      dispose() {
        closed = true;
        try {
          ws && ws.close();
        } catch {
          /* already gone */
        }
      },
      focus: () => input.focus(),
    };
  }

  return { mount };
})();
