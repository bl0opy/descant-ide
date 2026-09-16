// Pure render functions: data in, DOM out. No fetching, no state.
// Everything here serves the agent panel's log; the editor draws itself.
window.Views = (() => {
  const esc = (s) =>
    String(s == null ? '' : s).replace(
      /[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
    );

  // Event text should always be a string, but a malformed or unfamiliar
  // transcript line can carry structured payload. Never let that break the log.
  const asText = (v) =>
    typeof v === 'string'
      ? v
      : v == null
        ? ''
        : (() => {
            try {
              return JSON.stringify(v);
            } catch {
              return String(v);
            }
          })();

  function fmtTokens(n) {
    n = Math.round(Number(n) || 0);
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (Math.abs(n) >= 1000) return (n / 1000).toFixed(1) + 'k';
    return String(n);
  }

  // --- one-line summary of a tool call, like an IDE would show -----------
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
        return pick('file_path', 'path');
      case 'Edit':
        return pick('file_path');
      case 'Glob':
        return pick('pattern') + (input.path ? ` in ${input.path}` : '');
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

  function pretty(input) {
    try {
      return JSON.stringify(input, null, 2);
    } catch {
      return String(input);
    }
  }

  // ======================================================================
  // Agent log
  // ======================================================================

  /** Render one normalised event into the agent panel. Returns an element or null. */
  function renderEvent(ev, opts = {}) {
    const kind = ev.kind;
    const el = document.createElement('div');

    if (kind === 'tool_use') {
      el.className = 'tool';
      el.dataset.toolUseId = ev.tool_use_id || '';
      el._descantEvent = ev;
      const summary = toolSummary(ev.tool_name, ev.tool_input);
      // Edits and writes carry enough in their input to reconstruct a diff.
      const diffable =
        ['Edit', 'Write', 'NotebookEdit'].includes(ev.tool_name) && ev.tool_input;
      el.innerHTML = `
        <div class="tool-head">
          <span class="chevron">${window.Icons.chevronRight}</span>
          <span class="name">${esc(ev.tool_name || 'tool')}</span>
          <span class="arg">${esc(summary)}</span>
          ${diffable ? '<button class="tool-diff">diff</button>' : ''}
        </div>
        <div class="tool-body">${esc(pretty(ev.tool_input))}</div>`;
      el.querySelector('.tool-head').addEventListener('click', (e) => {
        if (e.target.closest('.tool-diff')) {
          e.stopPropagation();
          opts.onDiff?.(ev);
          return;
        }
        el.classList.toggle('open');
      });
      return el;
    }

    if (kind === 'tool_result') {
      // Fold results into the tool call they answer, when we can find it.
      const host = opts.container?.querySelector(
        `.tool[data-tool-use-id="${CSS.escape(ev.tool_use_id || '__none__')}"]`
      );
      const text = asText(ev.text) || '(no output)';
      if (host) {
        if (ev.is_error) host.classList.add('errored');
        const body = host.querySelector('.tool-body');
        body.textContent += `\n\n── result ──\n${text}`;
        return null;
      }
      el.className = 'tool' + (ev.is_error ? ' errored' : '');
      el.innerHTML = `
        <div class="tool-head">
          <span class="chevron">${window.Icons.chevronRight}</span>
          <span class="name">${esc(ev.tool_name || 'result')}</span>
          <span class="arg">${esc(text.split('\n')[0].slice(0, 120))}</span>
        </div>
        <div class="tool-body">${esc(text)}</div>`;
      el.querySelector('.tool-head').addEventListener('click', () =>
        el.classList.toggle('open')
      );
      return el;
    }

    if (kind === 'permission') {
      el.className = 'permission';
      const input = ev.meta?.tool_input;
      const rule = ev.meta?.rule;
      el.innerHTML = `
        <div class="ptitle">${window.Icons.warn} needs your decision</div>
        <div class="pbody">${esc(asText(ev.text))}</div>
        ${input ? `<div class="pcmd">${esc(toolSummary(ev.tool_name, input) || pretty(input))}</div>` : ''}`;

      // In a chat you can answer this; in the read-only log you cannot, and
      // pretending otherwise would be worse than saying so.
      if (opts.onPermission && ev.tool_use_id) {
        const actions = document.createElement('div');
        actions.className = 'pactions';
        actions.innerHTML = `
          ${rule ? `<code class="prule">${esc(rule)}</code>` : ''}
          <button class="btn small pallow">Allow once</button>
          <button class="btn small secondary palways">Always allow</button>
          <button class="btn small secondary pdeny">Deny</button>
          <span class="pstatus"></span>`;
        const status = actions.querySelector('.pstatus');
        const decide = async (decision, remember, label) => {
          actions.querySelectorAll('button').forEach((b) => (b.disabled = true));
          status.textContent = label;
          status.className = 'pstatus';
          try {
            await opts.onPermission(ev, decision, remember);
            status.textContent = decision === 'allow' ? 'allowed' : 'denied';
            status.className = 'pstatus ' + (decision === 'allow' ? 'ok' : 'no');
            el.classList.add('decided');
          } catch (err) {
            status.textContent = err.message;
            status.className = 'pstatus err';
            actions.querySelectorAll('button').forEach((b) => (b.disabled = false));
          }
        };
        actions
          .querySelector('.pallow')
          .addEventListener('click', () => decide('allow', false, 'resuming…'));
        actions
          .querySelector('.palways')
          .addEventListener('click', () => decide('allow', true, 'saving & resuming…'));
        actions
          .querySelector('.pdeny')
          .addEventListener('click', () => decide('deny', false, 'declining…'));
        el.appendChild(actions);
      } else {
        el.insertAdjacentHTML(
          'beforeend',
          `<div class="phint">Descant surfaces permission blocks instead of auto-approving them.
           Open this repo in a chat to decide here, or approve the tool in your Claude Code
           settings.</div>`
        );
      }
      return el;
    }

    if (kind === 'user_text') {
      el.className = 'ev ev-user_text';
      el.innerHTML = `<div class="who">you</div>${esc(asText(ev.text))}`;
      return el;
    }

    if (kind === 'assistant_text') {
      const text = asText(ev.text);
      if (!text.trim()) return null;
      el.className = 'ev ev-assistant_text';
      el.textContent = text;
      return el;
    }

    if (kind === 'thinking') {
      const text = asText(ev.text);
      if (!text.trim()) return null;
      el.className = 'ev ev-thinking';
      el.textContent = text;
      return el;
    }

    if (kind === 'error') {
      el.className = 'ev ev-error';
      el.textContent = asText(ev.text);
      return el;
    }

    if (kind === 'status') {
      // Usage/state chatter is noise in the log; the header shows it instead.
      if (['usage', 'state'].includes(ev.subtype)) return null;
      if (!asText(ev.text).trim() && ev.subtype !== 'closed') return null;
      el.className = 'ev ev-status';
      const label =
        ev.subtype === 'closed' ? 'done' : ev.subtype === 'result' ? 'result' : asText(ev.text);
      el.innerHTML = `<span class="rule"></span><span>${esc(label)}</span><span class="rule"></span>`;
      return el;
    }

    if (kind === 'system') {
      if (ev.subtype === 'usage') return null;
      const body = asText(ev.text);
      if (!body.trim()) return null;

      // Injected context (skill listings, tool listings, system reminders) is
      // often thousands of characters of machine-generated payload. Dumping it
      // into the log buries the actual conversation — which is the very problem
      // this app exists to point at. So: name it, size it, fold it away.
      const isAttachment = ev.subtype && !['task', 'stdout', 'rate_limit'].includes(ev.subtype);
      if (isAttachment && body.length > 160) {
        el.className = 'tool injected';
        el.innerHTML = `
          <div class="tool-head">
            <span class="chevron">${window.Icons.chevronRight}</span>
            <span class="name">${esc(ev.subtype)}</span>
            <span class="arg">injected · ~${fmtTokens(body.length / 3.5)} tokens</span>
          </div>
          <div class="tool-body">${esc(body)}</div>`;
        el.querySelector('.tool-head').addEventListener('click', () =>
          el.classList.toggle('open')
        );
        return el;
      }

      const label = ev.subtype && ev.subtype !== 'task' ? `${ev.subtype}: ` : '';
      el.className = 'ev ev-system';
      const shown = body.length > 300 ? `${body.slice(0, 300)}…` : body;
      el.textContent = `${label}${shown}`;
      return el;
    }

    return null;
  }

  return { esc, fmtTokens, renderEvent, toolSummary };
})();
