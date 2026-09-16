// The agent panel, as a terminal.
//
// This used to render a chat: bubbles, a "who" label on every turn, a card per
// tool call. That reads fine and works badly, because a conversation with a
// coding agent is mostly *tool traffic*, and tool traffic wants to look like
// what it is — a log. So the panel is now a transcript in the shell idiom:
// monospace throughout, one line per thing that happened, your turns marked
// with a caret and the agent's replies unadorned. Enter sends. Detail is one
// click away, never in your face.
//
// This module owns its own rendering (it used to borrow views.js) because the
// log and the conversation state are the same problem: what a tool_result does
// depends on the tool_use above it.
window.Chat = (() => {
  const esc = (s) =>
    String(s == null ? '' : s).replace(
      /[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
    );

  const el = (html) => {
    const d = document.createElement('div');
    d.innerHTML = html.trim();
    return d.firstElementChild;
  };

  const STATUS_LABEL = {
    idle: 'ready',
    running: 'working',
    needs_input: 'waiting on you',
    starting: 'starting',
    failed: 'failed',
    closed: 'closed',
  };

  const MODE_LABEL = {
    manual: 'ask me',
    acceptEdits: 'accept edits',
    plan: 'plan only',
    auto: 'auto',
    dontAsk: "don't ask",
  };

  const asText = (v) => {
    if (typeof v === 'string') return v;
    if (v == null) return '';
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  };

  /** The one line that says what a tool call is about to do. */
  function toolSummary(name, input) {
    if (!input || typeof input !== 'object') return '';
    const pick = (...keys) => {
      for (const k of keys) if (input[k]) return String(input[k]);
      return '';
    };
    switch (name) {
      case 'Bash':
        return pick('command');
      case 'Read':
      case 'Write':
      case 'NotebookEdit':
      case 'Edit':
        return pick('file_path', 'path');
      case 'Glob':
      case 'Grep':
        return pick('pattern') + (input.path ? ` in ${input.path}` : '');
      case 'WebFetch':
      case 'WebSearch':
        return pick('url', 'query');
      case 'Task':
      case 'Agent':
        return pick('description');
      default: {
        const first = Object.values(input)[0];
        return typeof first === 'string' ? first : JSON.stringify(input);
      }
    }
  }

  const pretty = (v) => {
    try {
      return JSON.stringify(v, null, 2);
    } catch {
      return String(v);
    }
  };

  /**
   * Drive one conversation through the panel's elements.
   *
   * Returns a handle: the caller decides when a chat is shown, replaced or torn
   * down, because one panel serves every folder you have open.
   */
  function attach({ chat, els, api, wsUrl, onDiff, onMeta, onOpenFile }) {
    let meta = chat;
    let ws = null;
    let disposed = false;
    let openAssistant = null;
    let lastInitSession = null;
    // Our own copy, so the panel can leave a chat and come back to it.
    const history = [];
    //: The permission row currently awaiting a keystroke, if any.
    let liveDecision = null;

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

    // ------------------------------------------------------------ rendering

    /** A collapsible one-liner: `⎿ Name  argument`, detail folded underneath. */
    function foldedLine({ name, arg, body, cls = '', actions = null }) {
      const node = el(`
        <div class="line tool ${cls}">
          <div class="line-head">
            <span class="gutter">&#9500;</span>
            <span class="tool-name">${esc(name)}</span>
            <span class="tool-arg">${esc(arg || '')}</span>
          </div>
          <pre class="line-body"></pre>
        </div>`);
      node.querySelector('.line-body').textContent = body || '';
      node.querySelector('.line-head').addEventListener('click', (e) => {
        if (e.target.closest('.line-act')) return;
        node.classList.toggle('open');
      });
      if (actions) node.querySelector('.line-head').appendChild(actions);
      return node;
    }

    function render(ev) {
      const kind = ev.kind;

      if (kind === 'user_text') {
        const node = el('<div class="line mine"><span class="gutter">&rsaquo;</span><span class="text"></span></div>');
        node.querySelector('.text').textContent = asText(ev.text);
        return node;
      }

      if (kind === 'assistant_text') {
        const text = asText(ev.text);
        if (!text.trim()) return null;
        const node = el('<div class="line say"></div>');
        node.textContent = text;
        return node;
      }

      if (kind === 'thinking') {
        const text = asText(ev.text);
        if (!text.trim()) return null;
        return foldedLine({ name: 'thinking', arg: firstLine(text), body: text, cls: 'muted' });
      }

      if (kind === 'tool_use') {
        const diffable = ['Edit', 'Write', 'NotebookEdit'].includes(ev.tool_name) && ev.tool_input;
        const path = ev.tool_input?.file_path || ev.tool_input?.path;
        let actions = null;
        if (diffable || path) {
          actions = el('<span class="line-acts"></span>');
          if (diffable) {
            const b = el('<button class="line-act">diff</button>');
            b.addEventListener('click', () => onDiff?.(ev));
            actions.appendChild(b);
          }
          if (path) {
            const b = el('<button class="line-act">open</button>');
            b.addEventListener('click', () => onOpenFile?.(path));
            actions.appendChild(b);
          }
        }
        const node = foldedLine({
          name: ev.tool_name || 'tool',
          arg: toolSummary(ev.tool_name, ev.tool_input),
          body: pretty(ev.tool_input),
          actions,
        });
        node.dataset.toolUseId = ev.tool_use_id || '';
        return node;
      }

      if (kind === 'tool_result') {
        // Fold the result into the call it answers, when we can find it. That
        // pairing is the whole reason this panel reads like a log: one line per
        // action, with its outcome inside it.
        const host = log.querySelector(
          `.tool[data-tool-use-id="${CSS.escape(ev.tool_use_id || '__none__')}"]`
        );
        const text = asText(ev.text) || '(no output)';
        if (host) {
          host.classList.add(ev.is_error ? 'errored' : 'done');
          host.querySelector('.line-body').textContent += `\n\n── result ──\n${text}`;
          const arg = host.querySelector('.tool-arg');
          if (ev.is_error) arg.textContent = `${arg.textContent} — failed`;
          return null;
        }
        return foldedLine({
          name: ev.tool_name || 'result',
          arg: firstLine(text),
          body: text,
          cls: ev.is_error ? 'errored' : '',
        });
      }

      if (kind === 'permission') return renderPermission(ev);

      if (kind === 'error') {
        const node = el('<div class="line err"></div>');
        node.textContent = asText(ev.text);
        return node;
      }

      if (kind === 'status') {
        if (['usage', 'state'].includes(ev.subtype)) return null;
        const text = asText(ev.text);
        if (!text.trim() && ev.subtype !== 'closed') return null;
        const label =
          ev.subtype === 'closed' ? 'done' : ev.subtype === 'result' ? 'result' : text;
        const node = el('<div class="line note"></div>');
        node.textContent = label;
        return node;
      }

      if (kind === 'system') {
        if (ev.subtype === 'usage') return null;
        const body = asText(ev.text);
        if (!body.trim()) return null;
        // Injected context (skill listings, tool schemas, system reminders) is
        // often thousands of characters of machine-generated payload. Name it,
        // size it, fold it away.
        const isAttachment = ev.subtype && !['task', 'stdout', 'rate_limit'].includes(ev.subtype);
        if (isAttachment && body.length > 160) {
          return foldedLine({
            name: ev.subtype,
            arg: `injected · ${Math.round(body.length / 1024)}k`,
            body,
            cls: 'muted',
          });
        }
        // Our own notices already read as sentences ("permission mode: plan"),
        // so prefixing them with the subtype says it twice.
        const OWN = ['task', 'permission_mode', 'model'];
        const node = el('<div class="line note"></div>');
        node.textContent = (ev.subtype && !OWN.includes(ev.subtype) ? `${ev.subtype}: ` : '') + body;
        return node;
      }

      return null;
    }

    const firstLine = (s) => (s || '').split('\n')[0].slice(0, 140);

    /**
     * A decision, answerable without reaching for the mouse.
     *
     * The keys are the point: in a panel that is trying to feel like a terminal,
     * "press a" beats "find the button". The buttons stay because a keyboard
     * shortcut nobody can see is not an affordance.
     */
    function renderPermission(ev) {
      const rule = ev.meta?.rule || '';
      const input = ev.meta?.tool_input;
      const node = el(`
        <div class="line perm">
          <div class="perm-head">needs your decision</div>
          <div class="perm-what">${esc(
            toolSummary(ev.tool_name, input) || asText(ev.text) || ev.tool_name || ''
          )}</div>
          ${rule ? `<code class="perm-rule">${esc(rule)}</code>` : ''}
          <div class="perm-acts">
            <button class="line-act" data-d="allow"><b>a</b>llow once</button>
            <button class="line-act" data-d="always"><b>A</b>lways allow</button>
            <button class="line-act" data-d="deny"><b>d</b>eny</button>
            <span class="perm-status"></span>
          </div>
        </div>`);

      if (!ev.tool_use_id) {
        node.querySelector('.perm-acts').innerHTML =
          '<span class="perm-status">this is a past transcript — nothing to decide</span>';
        return node;
      }

      const status = node.querySelector('.perm-status');
      const decide = async (decision, remember, label) => {
        if (liveDecision === decide) liveDecision = null;
        node.querySelectorAll('button').forEach((b) => (b.disabled = true));
        status.textContent = label;
        status.className = 'perm-status';
        try {
          const res = await api(`/api/chats/${meta.chat_id}/permission`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ tool_use_id: ev.tool_use_id, decision, remember }),
          });
          setMeta(res.meta || res);
          status.textContent = decision === 'allow' ? (remember ? 'always allowed' : 'allowed') : 'denied';
          status.className = 'perm-status ' + (decision === 'allow' ? 'ok' : 'no');
          node.classList.add('decided');
        } catch (err) {
          status.textContent = err.message;
          status.className = 'perm-status err';
          node.querySelectorAll('button').forEach((b) => (b.disabled = false));
        }
      };

      node.querySelector('[data-d="allow"]').addEventListener('click', () => decide('allow', false, 'resuming…'));
      node.querySelector('[data-d="always"]').addEventListener('click', () => decide('allow', true, 'saving & resuming…'));
      node.querySelector('[data-d="deny"]').addEventListener('click', () => decide('deny', false, 'declining…'));

      // Only the newest undecided prompt answers to the keyboard; answering an
      // old one by accident is worse than having to click.
      liveDecision = decide;
      return node;
    }

    /** Answer the live permission prompt from the keyboard. */
    function key(ch) {
      if (!liveDecision) return false;
      if (ch === 'a') return liveDecision('allow', false, 'resuming…'), true;
      if (ch === 'A') return liveDecision('allow', true, 'saving & resuming…'), true;
      if (ch === 'd') return liveDecision('deny', false, 'declining…'), true;
      return false;
    }

    // --------------------------------------------------------------- stream

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

      const node = render(ev);
      if (!node) {
        stick(was);
        return;
      }
      if (ev.kind === 'assistant_text') openAssistant = node;
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

    const call = (path, body) =>
      api(`/api/chats/${meta.chat_id}${path}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body || {}),
      });

    async function send(text) {
      text = (text || '').trim();
      if (!text) return;
      try {
        setMeta(await call('/send', { text }));
      } catch (err) {
        record({ kind: 'error', text: err.message, is_error: true });
        throw err;
      }
    }

    async function interrupt() {
      setMeta(await call('/interrupt'));
    }

    async function setMode(mode) {
      setMeta(await call('/mode', { permission_mode: mode }));
    }

    async function setModel(model) {
      setMeta(await call('/model', { model }));
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
      liveDecision = null;
      for (const ev of history) append(ev);
      log.scrollTop = log.scrollHeight;
    }

    connect();

    return {
      get meta() {
        return meta;
      },
      send,
      interrupt,
      setMode,
      setModel,
      key,
      repaint,
      // Exposed for headless verification, the same way app.js exposes its menu
      // handler: it draws an event without a live `claude` behind it.
      feed: record,
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

  return { attach, STATUS_LABEL, MODE_LABEL };
})();
