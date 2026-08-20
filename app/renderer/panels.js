// Loadout, skill mining and capability library views.
//
// Kept out of views.js (which renders transcripts) because these are a
// different job: they are about what a repo *carries*, not what a session did.
window.Panels = (() => {
  const { esc, fmtTokens } = window.Views;

  const el = (html) => {
    const d = document.createElement('div');
    d.innerHTML = html.trim();
    return d.firstElementChild;
  };

  // ======================================================================
  // Loadout — feature 4
  // ======================================================================

  /**
   * Per-repo tool loadout.
   *
   * The organising idea: every row shows what it costs you on *every turn*.
   * That number is the argument. An MCP server with 40 tools is not "one
   * checkbox", it is several thousand tokens of schema riding along in every
   * request whether or not you ever call it.
   */
  function renderLoadout(host, data, handlers) {
    host.innerHTML = '';
    const wrap = el('<div class="inspector"></div>');

    const t = data.totals;
    wrap.appendChild(
      el(`
      <div>
        <h1>Tool loadout</h1>
        <div class="subtitle">${esc(data.repo_path)}</div>
      </div>`)
    );

    wrap.appendChild(
      el(`
      <div class="stat-row">
        <div class="stat">
          <div class="stat-value">${fmtTokens(t.always_on_tokens)}</div>
          <div class="stat-label">loaded every turn</div>
        </div>
        <div class="stat">
          <div class="stat-value">${fmtTokens(t.mcp_active_tokens)}</div>
          <div class="stat-label">MCP schemas</div>
        </div>
        <div class="stat">
          <div class="stat-value">${fmtTokens(t.skill_active_tokens)}</div>
          <div class="stat-label">skill listings</div>
        </div>
      </div>`)
    );

    // --- MCP servers ---------------------------------------------------
    wrap.appendChild(el('<h2>MCP servers</h2>'));
    if (!data.mcp_servers.length) {
      wrap.appendChild(
        el(`<p class="muted">No MCP servers attached to this repo. Descant looks in
            <code>.mcp.json</code> and your per-project config.</p>`)
      );
    }

    for (const s of data.mcp_servers) {
      const cost = s.probed
        ? `${fmtTokens(s.token_cost)} tokens · ${s.tool_count} tools`
        : `<span class="unmeasured">unmeasured — ${esc(s.probe_error || 'not probed')}</span>`;
      const row = el(`
        <div class="loadout-row ${s.enabled ? '' : 'off'}">
          <button class="toggle ${s.enabled ? 'on' : ''}" title="${
            s.enabled ? 'Detach from this repo' : 'Attach to this repo'
          }"><span></span></button>
          <div class="loadout-main">
            <div class="loadout-name">${esc(s.name)}
              <span class="pill">${esc(s.source)}</span>
              <span class="pill">${esc(s.transport)}</span>
            </div>
            <div class="loadout-sub">${cost}</div>
            <div class="loadout-cmd">${esc(s.command)}</div>
          </div>
          <div class="loadout-actions"></div>
        </div>`);

      row.querySelector('.toggle').addEventListener('click', () =>
        handlers.toggleMcp(s.name, !s.enabled)
      );

      if (s.probed && s.tool_count) {
        const conv = el(
          `<button class="btn small">Convert to skill</button>`
        );
        conv.addEventListener('click', () => handlers.previewConversion(s.name));
        row.querySelector('.loadout-actions').appendChild(conv);
      }

      if (s.tools.length) {
        const details = el(`
          <div class="tool-list">
            ${s.tools
              .map(
                (tool) => `<div class="tool-list-row">
                  <span class="tname">${esc(tool.name)}</span>
                  <span class="tcost">${fmtTokens(tool.token_cost)}</span>
                </div>`
              )
              .join('')}
          </div>`);
        row.appendChild(details);
      }
      wrap.appendChild(row);
    }

    // --- skills ---------------------------------------------------------
    wrap.appendChild(
      el(`<h2>Skills <span class="h2-note">listing cost is charged every turn; body cost only when the skill fires</span></h2>`)
    );

    const sorted = [...data.skills].sort((a, b) => b.listing_tokens - a.listing_tokens);
    for (const s of sorted) {
      const row = el(`
        <div class="loadout-row ${s.enabled ? '' : 'off'}">
          <button class="toggle ${s.enabled ? 'on' : ''}"><span></span></button>
          <div class="loadout-main">
            <div class="loadout-name">${esc(s.name)}
              <span class="pill">${esc(s.source)}</span>
            </div>
            <div class="loadout-sub">
              <strong>${fmtTokens(s.listing_tokens)}</strong> every turn ·
              ${fmtTokens(s.body_tokens)} when invoked
            </div>
            <div class="loadout-desc">${esc(
              s.description.length > 220 ? s.description.slice(0, 220) + '…' : s.description
            )}</div>
          </div>
        </div>`);
      row.querySelector('.toggle').addEventListener('click', () =>
        handlers.toggleSkill(s.name, !s.enabled)
      );
      wrap.appendChild(row);
    }

    host.appendChild(wrap);
  }

  /** Preview dialog for an MCP -> skill conversion. */
  function renderConversionPreview(host, data, handlers) {
    host.innerHTML = '';
    const wrap = el('<div class="inspector"></div>');
    wrap.appendChild(
      el(`
      <div>
        <h1>Convert ${esc(data.server.name)} to a skill</h1>
        <div class="subtitle">
          Same capability, loaded on demand instead of on every turn.
        </div>
      </div>`)
    );

    wrap.appendChild(
      el(`
      <div class="stat-row">
        <div class="stat">
          <div class="stat-value">${fmtTokens(data.before_tokens)}</div>
          <div class="stat-label">now — MCP schemas</div>
        </div>
        <div class="stat accent">
          <div class="stat-value">${fmtTokens(data.after_tokens)}</div>
          <div class="stat-label">after — skill listing</div>
        </div>
        <div class="stat">
          <div class="stat-value">${data.ratio}&times;</div>
          <div class="stat-label">smaller</div>
        </div>
      </div>`)
    );

    wrap.appendChild(
      el(`<p class="muted">The server's ${data.server.tool_count} tool schemas stop being
          resident. The generated skill carries a short description and a working MCP
          client that lists and calls tools on demand.</p>`)
    );

    for (const [name, content] of Object.entries(data.skill.files)) {
      const block = el(`
        <div class="tool open">
          <div class="tool-head"><span class="name">${esc(name)}</span></div>
          <div class="tool-body">${esc(content)}</div>
        </div>`);
      wrap.appendChild(block);
    }

    const actions = el('<div class="composer-row" style="margin-top:var(--space-6)"></div>');
    const go = el('<button class="btn">Write skill &amp; detach server</button>');
    go.addEventListener('click', () => handlers.confirmConversion(data.server.name));
    const cancel = el('<button class="btn secondary">Back</button>');
    cancel.addEventListener('click', handlers.cancel);
    actions.appendChild(go);
    actions.appendChild(cancel);
    wrap.appendChild(actions);
    host.appendChild(wrap);
  }

  // ======================================================================
  // Skill mining — feature 3
  // ======================================================================

  function renderMining(host, data, handlers) {
    host.innerHTML = '';
    const wrap = el('<div class="inspector"></div>');
    wrap.appendChild(
      el(`
      <div>
        <h1>Mined workflows</h1>
        <div class="subtitle">
          Tool sequences that recurred often enough to be worth naming, found by
          diffing ${data.candidate_count ? '' : ''}your transcript history.
        </div>
      </div>`)
    );

    if (!data.candidates.length) {
      wrap.appendChild(
        el(`<p class="muted">Nothing repeated often enough yet. The miner wants a
            sequence seen 3+ times, or in 2+ separate sessions, and containing at
            least one command — reading and editing files is what every session
            does, so it never counts as a workflow.</p>`)
      );
      host.appendChild(wrap);
      return;
    }

    wrap.appendChild(
      el(`<p class="muted">Sequences seen in more than one session rank highest: repetition
          inside a single session is usually a retry loop, whereas the same steps across
          sessions is a habit worth capturing.</p>`)
    );

    for (const c of data.candidates) {
      const steps = c.sequence
        .map((s) => `<span class="step">${esc(s)}</span>`)
        .join('<span class="arrow">→</span>');

      const card = el(`
        <div class="mine-card">
          <div class="mine-head">
            <div class="mine-flow">${steps}</div>
            <div class="mine-stats">
              <span class="pill ${c.session_count > 1 ? 'measured' : ''}">
                ${c.occurrences}× in ${c.session_count} session${c.session_count === 1 ? '' : 's'}
              </span>
              <span class="pill">~${fmtTokens(c.estimated_savings)} saved</span>
            </div>
          </div>
          <div class="mine-body">
            <div class="mine-repos">${c.repos.map((r) => esc(r.split('/').pop())).join(', ')}</div>
            ${
              c.example_commands.length
                ? `<pre class="mine-cmds">${c.example_commands
                    .slice(0, 4)
                    .map(esc)
                    .join('\n')}</pre>`
                : ''
            }
          </div>
          <div class="mine-actions">
            <button class="btn small secondary preview">View SKILL.md</button>
            <button class="btn small accept">Create skill</button>
          </div>
          <div class="tool"><div class="tool-body">${esc(
            c.proposed_skill.skill_md
          )}</div></div>
        </div>`);

      const toolBlock = card.querySelector('.tool');
      card.querySelector('.preview').addEventListener('click', () =>
        toolBlock.classList.toggle('open')
      );
      card.querySelector('.accept').addEventListener('click', () =>
        handlers.acceptMined(c)
      );
      wrap.appendChild(card);
    }
    host.appendChild(wrap);
  }

  // ======================================================================
  // Capability library — feature 5
  // ======================================================================

  function renderLibrary(host, data, handlers) {
    host.innerHTML = '';
    const wrap = el('<div class="inspector"></div>');
    const s = data.stats;

    wrap.appendChild(
      el(`
      <div>
        <h1>Capability library</h1>
        <div class="subtitle">
          ${s.skill_count} skills and ${s.mcp_tool_count} MCP tools across
          ${data.repos.length} repo${data.repos.length === 1 ? '' : 's'},
          searchable so tools can be found on demand rather than all declared up front.
        </div>
      </div>`)
    );

    const search = el(`
      <div class="lib-search">
        <input id="lib-q" type="text" placeholder="Describe the task — e.g. “turn this csv into a chart”"
               value="${esc(data.query || '')}" />
        <button class="btn" id="lib-go">Search</button>
      </div>`);
    wrap.appendChild(search);

    wrap.appendChild(
      el(`<p class="muted retrieval-note">Ranking is BM25 over name, description and body
          plus a synonym map — <strong>lexical, not embeddings</strong>. It behaves
          semantically where the synonyms reach (“test” finds pytest) and lexically
          elsewhere.</p>`)
    );

    if (!data.results.length) {
      wrap.appendChild(el(`<p class="muted">No matches.</p>`));
    }

    for (const r of data.results) {
      const row = el(`
        <div class="lib-row">
          <span class="lib-kind ${r.kind}">${r.kind === 'skill' ? 'skill' : 'mcp'}</span>
          <div class="lib-main">
            <div class="lib-name">${esc(r.name)}
              ${r.server ? `<span class="pill">${esc(r.server)}</span>` : ''}
              <span class="pill">${esc(r.source)}</span>
            </div>
            <div class="lib-desc">${esc(
              (r.description || '').length > 200
                ? r.description.slice(0, 200) + '…'
                : r.description || ''
            )}</div>
            ${
              r.matched && r.matched.length
                ? `<div class="lib-matched">matched ${r.matched
                    .map((m) => `<code>${esc(m)}</code>`)
                    .join(' ')}</div>`
                : ''
            }
          </div>
          <div class="lib-cost">${fmtTokens(r.always_on_tokens)}</div>
        </div>`);
      wrap.appendChild(row);
    }

    host.appendChild(wrap);

    const run = () => handlers.search(host.querySelector('#lib-q').value);
    host.querySelector('#lib-go').addEventListener('click', run);
    host.querySelector('#lib-q').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') run();
    });
  }

  return { renderLoadout, renderConversionPreview, renderMining, renderLibrary };
})();
