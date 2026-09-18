/*
 * Purpose: All portal behaviour — screen routing, hub picker, dashboard, connections, the
 * connection detail screen and the 5-step onboarding wizard — talking to the real backend over
 * /api/*. One global `m2mV2` with init(), matching FR-06 §13. Pure display rules live in
 * portal_state.js so they can be tested under Node.
 *
 * Secrets are never rendered (FR-06 §12.1): the robot email and Client ID are shown because the
 * admin must paste them into ACC. A Databricks service-principal secret is typed, POSTed once
 * and never read back — it is held in a JS variable, never written into the DOM.
 */
(function () {
  const S = {
    me: null, hub: null, view: 'dashboard', status: null, busy: false,
    connections: [], connection: null, projects: null, catalogs: null,
    // Wizard step-3 form. `dbx_client_secret` lives here and only here.
    form: { project_id: '', project_name: '', robot_verified: false, workspace_url: '',
            dbx_client_id: '', dbx_client_secret: '', catalog: '' },
    step: null, poll: null,
  };
  const el = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const hubPath = () => encodeURIComponent(S.hub.hub_id);

  // ── plumbing ────────────────────────────────────────────────
  let toastTimer = null;
  function toast(message) {
    const t = el('toast');
    t.textContent = '✦ ' + message;
    t.style.opacity = '1';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.style.opacity = '0'; }, 3400);
  }

  async function api(path, options) {
    const resp = await fetch(path, Object.assign(
      { headers: { 'Content-Type': 'application/json' } }, options || {},
    ));
    let body = {};
    try { body = await resp.json(); } catch (_) { /* empty body is fine */ }
    if (!resp.ok) {
      const err = new Error(body.error || `Request failed (${resp.status})`);
      err.status = resp.status;
      err.reason = body.reason;
      err.body = body;
      throw err;
    }
    return body;
  }

  function screen(name) {
    ['login', 'denied', 'hub-picker', 'app'].forEach((s) => {
      el('screen-' + s).classList.toggle('hidden', s !== name);
    });
  }

  /* Only one poller may exist at a time — leaving one running behind a view change would
     keep hitting the API forever and fight the next screen for #main-view. */
  function stopPolling() {
    if (S.poll) { clearInterval(S.poll); S.poll = null; }
  }

  function startPolling(fn, everyMs) {
    stopPolling();
    S.poll = setInterval(fn, everyMs);
  }

  /* Wizard state across the Databricks authorize redirect. sessionStorage, not the URL: the
     step-3 form holds a project id and a workspace URL, and it dies with the tab. The typed
     service-principal secret is excluded — it is never persisted anywhere client-side. */
  const RESUME_KEY = 'm2mV2.dbxResume';

  function stashResume() {
    sessionStorage.setItem(RESUME_KEY, JSON.stringify({
      hub_id: S.hub.hub_id,
      step: 3,
      form: Object.assign({}, S.form, { dbx_client_secret: '' }),
    }));
  }

  function takeResume() {
    const raw = sessionStorage.getItem(RESUME_KEY);
    sessionStorage.removeItem(RESUME_KEY);  // one shot — a later reload must not resurrect it
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (_) { return null; }
  }

  // ── boot ────────────────────────────────────────────────────
  async function init() {
    // Read once, here: the hub it belongs to is only known inside selectHub().
    const stash = takeResume();
    try {
      S.me = await api('/api/me');
    } catch (err) {
      if (err.status === 401) { screen('login'); return; }
      showDenied('Something went wrong', err.message, { retry: true });
      return;
    }

    if (S.me.access === 'denied') {
      // A definitive "not a hub admin" — FR-06 §4.2.
      showDenied('Access Restricted', S.me.message);
      return;
    }
    if (S.me.access === 'unverified' || S.me.access === 'unavailable') {
      // We could not ask, which is not the same as "no". Never tell a real admin they are
      // not one because Autodesk was unreachable or the Client ID is not whitelisted yet.
      showDenied("We couldn't verify your access", S.me.message, {
        retry: true,
        remedy: S.me.aps_client_id
          ? `In ACC, go to <strong>Account Admin ▸ Settings ▸ Custom Integrations</strong> and add
             Client ID <span class="font-mono break-all">${esc(S.me.aps_client_id)}</span>, then try again.`
          : '',
      });
      return;
    }

    if (!S.me.hubs.length) { showDenied('Access Restricted', 'No hubs available.'); return; }

    const active = S.me.hubs.find((h) => h.hub_id === S.me.active_hub_id);
    if (active) { await selectHub(active.hub_id, stash); }
    else if (S.me.hubs.length === 1) { await selectHub(S.me.hubs[0].hub_id, stash); }
    else { showHubPicker(); }
  }

  function showDenied(title, message, opts) {
    const o = opts || {};
    el('denied-title').textContent = title;
    el('denied-message').textContent = message || '';
    const remedy = el('denied-remedy');
    remedy.classList.toggle('hidden', !o.remedy);
    if (o.remedy) remedy.innerHTML = o.remedy;
    const retry = el('denied-retry');
    retry.classList.toggle('hidden', !o.retry);
    retry.onclick = () => location.reload();
    el('denied-icon').className = o.retry
      ? 'w-14 h-14 rounded-full bg-amber-50 mx-auto mb-5 flex items-center justify-center'
      : 'w-14 h-14 rounded-full bg-rose-50 mx-auto mb-5 flex items-center justify-center';
    screen('denied');
  }

  // ── hub picker ──────────────────────────────────────────────
  function showHubPicker() {
    stopPolling();
    el('hub-cards').innerHTML = S.me.hubs.map((h) => {
      const badge = portalState.tenantBadge(h.onboarding_status);
      const meta = !h.onboarding_status
        ? 'Brand-new hub — first connection'
        : `${h.connection_count} connection(s)`;
      const cta = portalState.needsSetup(h) ? 'Continue setup →' : 'Manage →';
      return `
        <button onclick="m2mV2.selectHub('${esc(h.hub_id)}')"
                class="text-left bg-white rounded-xl border border-slate-200 p-6
                       hover:border-primary-500 hover:shadow-md transition group">
          <div class="flex items-start justify-between gap-3 mb-2">
            <div class="min-w-0">
              <div class="font-semibold truncate">${esc(h.name)}</div>
              <div class="font-mono text-xs text-slate-500 truncate">${esc(h.id_short)}…</div>
            </div>
            <span class="px-2 py-0.5 rounded-full text-xs font-medium ${badge.cls}">${esc(badge.label)}</span>
          </div>
          <div class="text-sm text-slate-600 mb-4">${esc(meta)}</div>
          <div class="text-sm font-medium text-slate-700 group-hover:text-primary-600">${cta}</div>
        </button>`;
    }).join('');
    screen('hub-picker');
  }

  async function selectHub(hubId, stash) {
    try {
      await api('/api/session/hub', { method: 'POST', body: JSON.stringify({ hub_id: hubId }) });
    } catch (err) {
      toast(err.message);
      if (err.reason === 'not_hub_admin') showDenied('Access Restricted', err.message);
      return;
    }
    S.hub = S.me.hubs.find((h) => h.hub_id === hubId);
    // Switching hub must not carry the previous hub's connection or wizard state.
    S.connection = null;
    S.projects = null;
    S.catalogs = null;
    S.step = null;
    resetForm();
    // Coming back from the Databricks authorize redirect: put the admin back on step 3 with
    // what they had typed, and list the catalogs the round trip just bought them.
    const resume = portalState.resumeAfterDatabricks(location.search, stash, hubId);
    if (resume) {
      S.step = resume.step;
      Object.assign(S.form, resume.form);
    }
    await Promise.all([refreshStatus(), refreshConnections()]);
    renderShell();
    // A hub that still needs setup lands on the wizard, not an empty dashboard (FR-06 §3.3).
    go((S.step || portalState.needsSetup(S.status)) ? 'setup' : 'dashboard');
    screen('app');
    if (resume) loadCatalogs();
  }

  function resetForm() {
    S.form = { project_id: '', project_name: '', robot_verified: false, workspace_url: '',
               dbx_client_id: '', dbx_client_secret: '', catalog: '' };
  }

  async function refreshStatus() {
    try {
      S.status = await api(`/api/tenants/${hubPath()}/ssa/status`);
    } catch (err) {
      S.status = null;
      toast(err.message);
    }
  }

  async function refreshConnections() {
    try {
      const body = await api('/api/connections');
      S.connections = body.connections || [];
      S.nextSteps = body.next_steps || {};
    } catch (err) {
      S.connections = [];
      toast(err.message);
    }
  }

  function renderShell() {
    const badge = portalState.tenantBadge(S.status && S.status.onboarding_status);
    el('hub-name').textContent = S.hub.name;
    el('hub-id').textContent = S.hub.id_short + '…';
    el('hub-badge').textContent = badge.label;
    el('hub-badge').className =
      'inline-block mt-2 px-2 py-0.5 rounded-full text-xs font-medium ' + badge.cls;
    el('user-initials').textContent = (S.me.user_id || '?').slice(0, 2).toUpperCase();
    el('user-name').textContent = S.me.user_id;

    el('nav').innerHTML = [
      ['dashboard', 'Dashboard'], ['connections', 'Connections'],
      ['setup', 'Setup'], ['help', 'Help'],
    ].map(([id, label]) => `
        <button onclick="m2mV2.go('${id}')" ${S.view === id ? 'aria-current="page"' : ''}
                class="w-full text-left px-3 py-2 rounded-lg ${S.view === id
                  ? 'bg-primary-50 text-primary-600 font-medium' : 'hover:bg-slate-50'}">
          ${label}
        </button>`).join('');
  }

  function go(view) {
    stopPolling();
    S.view = view;
    renderShell();
    const target = el('main-view');
    target.className = 'fade-in';
    if (view === 'setup') renderSetup(target);
    else if (view === 'connections') renderConnections(target);
    else if (view === 'detail') renderDetail(target);
    else if (view === 'help') renderHelp(target);
    else renderDashboard(target);
  }

  // ── views ───────────────────────────────────────────────────
  function panel(title, body) {
    return `<div class="bg-white rounded-xl border border-slate-200 p-6 mb-6">
      <h2 class="text-lg font-semibold mb-4">${title}</h2>${body}</div>`;
  }

  function notice(tone, body) {
    const tones = {
      amber: 'bg-amber-50 border-amber-200 text-amber-900',
      rose: 'bg-rose-50 border-rose-200 text-rose-900',
      emerald: 'bg-emerald-50 border-emerald-200 text-emerald-900',
      blue: 'bg-primary-50 border-primary-100 text-slate-700',
    };
    return `<div class="border rounded-lg p-4 text-sm mb-4 ${tones[tone]}">${body}</div>`;
  }

  function copyRow(value) {
    return `<div class="flex items-center gap-2 mb-3">
      <code class="flex-1 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2
                   font-mono text-xs break-all">${esc(value || '—')}</code>
      <button onclick="m2mV2.copy('${esc(value || '')}')"
              class="px-4 py-2.5 border border-slate-200 rounded-lg text-sm font-medium
                     hover:bg-slate-50">Copy</button>
    </div>`;
  }

  function renderDashboard(target) {
    const st = S.status || {};
    const robot = st.robot;
    const ready = S.connections.filter((c) => c.onboarding_status === 'ready').length;
    const scheduled = S.connections.filter((c) => c.cdc_enabled).length;
    const stat = (label, value) => `
      <div class="bg-white rounded-xl border border-slate-200 p-6">
        <div class="text-xs uppercase tracking-wide text-slate-500 mb-1">${label}</div>
        <div class="text-2xl font-semibold">${esc(value)}</div>
      </div>`;

    target.innerHTML = `
      <h1 class="text-2xl font-semibold mb-1">${esc(S.hub.name)}</h1>
      <p class="text-sm text-slate-600 mb-8">
        Service Account ${robot ? 'active' : 'not provisioned'}
      </p>
      ${portalState.needsSetup(st) ? `
        <div class="bg-amber-50 border border-amber-200 rounded-lg p-4 text-sm text-amber-900 mb-6
                    flex items-center justify-between gap-4">
          <span>Onboarding incomplete. The hub service account / whitelist is not ready.</span>
          <button onclick="m2mV2.go('setup')"
                  class="px-4 py-2 bg-primary-600 text-white rounded-lg text-sm font-medium
                         hover:bg-primary-700">Resume setup</button>
        </div>` : ''}
      <div class="grid sm:grid-cols-3 gap-4 mb-6">
        ${stat('Connections', S.connections.length)}
        ${stat('Ready', ready)}
        ${stat('Scheduled', scheduled)}
      </div>
      ${panel('Service Account', robot ? `
        <div class="text-sm space-y-2">
          <div><span class="text-slate-500">Robot email</span>
            <div class="font-mono text-xs break-all">${esc(robot.email)}</div></div>
          <div class="text-slate-500 text-xs">Private key stored securely in vault</div>
        </div>` : `<p class="text-sm text-slate-600">Not provisioned yet.</p>`)}
      <div class="bg-primary-50 border border-primary-100 rounded-lg p-4 text-sm text-slate-700">
        <strong>Headless sync:</strong> scheduled jobs use this hub's Service Account and each
        connection's Databricks Service Principal. No human login is required — the running
        credential is re-minted from the vault when it expires.
      </div>`;
  }

  // ── connections list (FR-06 §4.6) ───────────────────────────
  function renderConnections(target) {
    const header = `
      <div class="flex items-start justify-between gap-4 mb-6">
        <div>
          <h1 class="text-2xl font-semibold mb-1">Connections</h1>
          <p class="text-sm text-slate-600">
            One connection per ACC project and Databricks catalog. All of this hub's admins
            share them.
          </p>
        </div>
        <button onclick="m2mV2.newConnection()"
                class="px-4 py-2.5 bg-primary-600 text-white rounded-lg text-sm font-medium
                       hover:bg-primary-700 shrink-0">+ New connection</button>
      </div>`;

    if (!S.connections.length) {
      target.innerHTML = header + `
        <div class="bg-white rounded-xl border border-slate-200 p-12 text-center">
          <div class="font-semibold mb-1">No connections yet</div>
          <p class="text-sm text-slate-600 mb-6">
            Pick an ACC project and a Databricks catalog to install the first pipeline.
          </p>
          <button onclick="m2mV2.newConnection()"
                  class="px-4 py-2.5 bg-primary-600 text-white rounded-lg text-sm font-medium
                         hover:bg-primary-700">Start setup</button>
        </div>`;
      return;
    }

    target.innerHTML = header + `<div class="space-y-3">${S.connections.map((c) => `
      <button onclick="m2mV2.openConnection('${esc(c.connection_id)}')"
              class="w-full text-left bg-white rounded-xl border border-slate-200 p-5
                     hover:border-primary-500 hover:shadow-md transition">
        <div class="flex items-start justify-between gap-3 mb-2">
          <div class="min-w-0">
            <div class="font-semibold truncate">
              ${esc(c.project_name || c.project_id)}
            </div>
            <div class="font-mono text-xs text-slate-500 truncate">
              ${esc(c.catalog)} · ${esc(c.dbx_workspace_url)}
            </div>
          </div>
          <span class="px-2 py-0.5 rounded-full text-xs font-medium shrink-0
                       ${portalState.statusBadge(c.onboarding_status)}">
            ${esc(String(c.onboarding_status).replace(/_/g, ' '))}
          </span>
        </div>
        <div class="text-xs text-slate-500">
          Auto CDC: ${esc(portalState.nextRunLabel(c))}
        </div>
      </button>`).join('')}</div>`;
  }

  // ── connection detail (FR-06 §4.7) ──────────────────────────
  function renderDetail(target) {
    const d = S.connection;
    if (!d) { go('connections'); return; }
    const c = d.connection;
    const bs = d.bootstrap || {};
    const run = d.latest_run;
    const pill = portalState.runPill(run && run.state);
    const blocker = portalState.autoCdcBlocker({
      onboarding_status: c.onboarding_status,
      has_service_principal: d.has_service_principal,
      // The robot check is a live API call, so the toggle offers it rather than
      // pre-asserting it; the server refuses and names it if it is not satisfied.
      robot_verified: true,
    });
    const keyAgeDays = portalState.keyAgeWarning(S.status && S.status.robot);
    const syncing = portalState.isRunInFlight(run);
    const field = (label, value) => `
      <div><span class="text-slate-500 text-xs">${label}</span>
        <div class="font-mono text-xs break-all">${esc(value || '—')}</div></div>`;

    target.innerHTML = `
      <button onclick="m2mV2.go('connections')"
              class="text-sm text-slate-500 hover:text-slate-900 mb-4">← Connections</button>
      <div class="flex items-start justify-between gap-4 mb-6">
        <div class="min-w-0">
          <h1 class="text-2xl font-semibold mb-1 truncate">
            ${esc(c.project_name || c.project_id)}
          </h1>
          <p class="text-sm text-slate-600 font-mono break-all">${esc(c.connection_id)}</p>
        </div>
        <span class="px-2 py-0.5 rounded-full text-xs font-medium shrink-0
                     ${portalState.statusBadge(c.onboarding_status)}">
          ${esc(String(c.onboarding_status).replace(/_/g, ' '))}
        </span>
      </div>

      ${keyAgeDays ? notice('amber', `The hub's robot signing key is ${keyAgeDays} days old.
        Rotate it to stay inside the 90-day policy.
        <button onclick="m2mV2.rotateKey()" class="underline font-medium">Rotate robot key</button>`) : ''}

      ${panel('Target', `<div class="grid sm:grid-cols-2 gap-4 text-sm">
        ${field('ACC project', c.project_id)}
        ${field('Databricks workspace', c.dbx_workspace_url)}
        ${field('Unity Catalog', c.catalog)}
        ${field('Volume', bs.volume_path)}
        ${field('Snapshot pipeline', c.snapshot_pipeline_id)}
        ${field('CDC pipeline', c.cdc_pipeline_id)}
      </div>`)}

      ${panel('Databricks Service Principal', d.has_service_principal ? `
        ${notice('emerald', `Service principal connected —
          <span class="font-mono">${esc(d.dbx_client_id)}</span>.
          Secret stored in vault, never displayed.`)}
        <button onclick="m2mV2.showSpForm()"
                class="px-4 py-2.5 border border-slate-200 rounded-lg text-sm font-medium
                       hover:bg-slate-50">Replace secret</button>` : `
        <p class="text-sm text-slate-600 mb-4">
          Scheduled sync needs a service principal. Manual sync can use your own Databricks
          sign-in.
        </p>
        <button onclick="m2mV2.showSpForm()"
                class="px-4 py-2.5 bg-primary-600 text-white rounded-lg text-sm font-medium
                       hover:bg-primary-700">Add service principal</button>`) +
        `<div id="sp-form" class="hidden mt-4"></div>`}

      ${panel('Scheduled sync (Auto CDC)', `
        <div class="flex items-start justify-between gap-4">
          <div class="text-sm">
            <div class="font-medium mb-1">Daily CDC sync</div>
            <div class="text-slate-600">
              Next run: ${esc(portalState.nextRunLabel(c))}
            </div>
          </div>
          <button onclick="m2mV2.toggleSchedule(${c.cdc_enabled ? 'false' : 'true'})"
                  ${blocker ? 'disabled' : ''}
                  title="${esc(blocker ? portalState.AUTO_CDC_MESSAGES[blocker] : '')}"
                  class="px-4 py-2.5 rounded-lg text-sm font-medium shrink-0 ${blocker
                    ? 'bg-slate-100 text-slate-400 cursor-not-allowed'
                    : c.cdc_enabled ? 'border border-slate-200 hover:bg-slate-50'
                                    : 'bg-primary-600 text-white hover:bg-primary-700'}">
            ${c.cdc_enabled ? 'Turn off' : 'Turn on'}
          </button>
        </div>
        ${blocker ? notice('amber', esc(portalState.AUTO_CDC_MESSAGES[blocker])).replace('mb-4', 'mt-4 mb-0') : ''}`)}

      ${panel('Sync', `
        <div class="flex items-center gap-3 mb-4">
          <button onclick="m2mV2.startSync('snapshot')" ${syncing ? 'disabled' : ''}
                  class="px-4 py-2.5 rounded-lg text-sm font-medium ${syncing
                    ? 'bg-slate-100 text-slate-400 cursor-not-allowed'
                    : 'bg-emerald-600 text-white hover:bg-emerald-700'}">
            ▶ Sync Snapshot</button>
          <button onclick="m2mV2.startSync('cdc')" ${syncing ? 'disabled' : ''}
                  class="px-4 py-2.5 border border-slate-200 rounded-lg text-sm font-medium
                         ${syncing ? 'text-slate-400 cursor-not-allowed' : 'hover:bg-slate-50'}">
            ▶ Sync CDC</button>
          <button onclick="m2mV2.refreshDetail()"
                  class="px-4 py-2.5 border border-slate-200 rounded-lg text-sm font-medium
                         hover:bg-slate-50">Refresh status</button>
        </div>
        <div id="run-card">${runCard(run)}</div>`)}

      ${panel('Recent runs', `<div id="run-history">
        <p class="text-sm text-slate-500">Loading…</p></div>`)}`;

    loadRuns();
    if (syncing) startPolling(refreshDetail, 10000);
  }

  function runCard(run) {
    if (!run) {
      return `<p class="text-sm text-slate-600">No runs yet for this connection.</p>`;
    }
    const pill = portalState.runPill(run.state);
    const files = run.files_count != null ? run.files_count : filesFromCounts(run.record_counts);
    return `
      <div class="border border-slate-200 rounded-lg p-4 text-sm">
        <div class="flex items-center gap-3 mb-2">
          <span class="px-2 py-0.5 rounded-full text-xs font-medium ${pill.cls}">
            ${esc(pill.label)}</span>
          <span class="text-slate-500 text-xs">
            ${esc(run.mode)} · ${esc(run.trigger_type)} · started ${esc(portalState.rel(run.started_at))}
          </span>
        </div>
        ${files != null ? `<div class="text-slate-600">${esc(files)} CSV files uploaded</div>` : ''}
        ${run.phase && run.state === 'failed'
          ? `<div class="text-xs text-slate-500 mt-1">failed during ${esc(run.phase)}</div>` : ''}
        ${run.error ? `<div class="mt-2 bg-rose-50 border border-rose-200 rounded p-2
          font-mono text-xs text-rose-900 break-all">${esc(run.error)}</div>` : ''}
        ${portalState.isRunInFlight(run)
          ? `<div class="text-xs text-slate-500 mt-2">Polling every 10s…</div>` : ''}
      </div>`;
  }

  function filesFromCounts(raw) {
    if (!raw) return null;
    try { return JSON.parse(raw).files; } catch (_) { return null; }
  }

  async function loadRuns() {
    const box = el('run-history');
    if (!box) return;
    try {
      const body = await api(
        `/api/connections/${encodeURIComponent(S.connection.connection.connection_id)}/runs?limit=10`,
      );
      if (!body.runs.length) {
        box.innerHTML = `<p class="text-sm text-slate-600">No runs yet.</p>`;
        return;
      }
      box.innerHTML = `<div class="divide-y divide-slate-100 text-sm">${body.runs.map((r) => {
        const pill = portalState.runPill(r.state);
        return `<div class="py-2 flex items-center justify-between gap-3">
          <span class="text-slate-600">
            ${esc(r.mode)} · ${esc(r.trigger_type)} · ${esc(portalState.rel(r.started_at))}
          </span>
          <span class="px-2 py-0.5 rounded-full text-xs font-medium ${pill.cls} shrink-0">
            ${esc(pill.label)}</span>
        </div>`;
      }).join('')}</div>`;
    } catch (err) {
      box.innerHTML = `<p class="text-sm text-rose-700">${esc(err.message)}</p>`;
    }
  }

  // ── setup wizard (FR-06 §4.8) ───────────────────────────────
  function currentStep() {
    if (S.step) return S.step;
    const hubStep = portalState.setupStepFor(S.status && S.status.onboarding_status);
    if (hubStep < 3) return hubStep;
    if (S.connection) {
      return portalState.connectionStepFor(S.connection.connection.onboarding_status);
    }
    return 3;
  }

  function renderSetup(target) {
    const st = S.status || {};
    const step = currentStep();
    const done = (n) => step > n;
    const stepper = ['Whitelist', 'SSA', 'Target', 'Bootstrap', 'Sync'].map((label, i) => {
      const n = i + 1;
      const cls = done(n) ? 'bg-emerald-500 text-white'
        : n === step ? 'bg-primary-600 text-white' : 'bg-slate-200 text-slate-500';
      return `<div class="flex items-center gap-2">
        <span class="w-6 h-6 rounded-full text-xs flex items-center justify-center ${cls}">
          ${done(n) ? '✓' : n}</span>
        <span class="text-sm ${n === step ? 'font-medium' : 'text-slate-500'}">${label}</span>
      </div>`;
    }).join('<div class="flex-1 h-px bg-slate-200 mx-2"></div>');

    target.innerHTML = `
      <h1 class="text-2xl font-semibold mb-1">Setup Wizard</h1>
      <p class="text-sm text-slate-600 mb-6">
        Onboarding for <strong>${esc(S.hub.name)}</strong> — hub-scoped, one robot per hub.
      </p>
      <div class="flex items-center mb-8">${stepper}</div>
      <div id="step-body"></div>`;

    const body = el('step-body');
    if (step === 1) renderWhitelistStep(body, st);
    else if (step === 2) renderSsaStep(body, st);
    else if (step === 3) renderTargetStep(body, st);
    else if (step === 4) renderBootstrapStep(body);
    else renderSyncStep(body);
  }

  function renderWhitelistStep(body, st) {
    body.innerHTML = panel('Custom Integration Whitelist', `
      <p class="text-sm text-slate-600 mb-4">
        This step cannot be automated. The Client ID must be whitelisted in Autodesk before any
        ACC data can flow.
      </p>
      ${notice('amber', `<div class="font-medium mb-1">ACTION REQUIRED</div>
        Account Admin ▸ Settings ▸ Custom Integrations ▸ add the connector Client ID.`)}
      ${copyRow(st.aps_client_id)}
      <div id="verify-result"></div>
      <button onclick="m2mV2.verifyWhitelist()"
              class="px-4 py-2.5 bg-primary-600 text-white rounded-lg text-sm font-medium
                     hover:bg-primary-700">Verify whitelist</button>`);
  }

  function renderSsaStep(body, st) {
    const robot = st.robot;
    if (st.capacity === 'capacity_exhausted' || st.onboarding_status === 'ssa_limit_reached') {
      body.innerHTML = panel('Service Account', `
        <div class="bg-rose-50 border border-rose-200 rounded-lg p-4 text-sm text-rose-900">
          <div class="font-medium mb-1">SSA limit reached</div>
          Provisioning capacity has been reached and our team has been notified. Creation is
          intentionally not attempted — the cap sits at the Client ID level across the org.
        </div>`);
      return;
    }
    body.innerHTML = panel('Service Account', robot ? `
      ${notice('emerald', 'Service Account ready (reused — no second robot)')}
      <div class="text-sm mb-2">Invite this robot to your ACC project:</div>
      ${copyRow(robot.email)}
      <p class="text-xs text-slate-500 mb-4">Private key stored securely in vault.</p>
      <button onclick="m2mV2.gotoStep(3)"
              class="px-4 py-2.5 bg-primary-600 text-white rounded-lg text-sm font-medium
                     hover:bg-primary-700">Continue →</button>` : `
      <p class="text-sm text-slate-600 mb-4">No robot exists for this hub yet.</p>
      ${st.last_error ? `<div class="bg-rose-50 border border-rose-200 rounded-lg p-3
        font-mono text-xs text-rose-900 mb-4 break-all">${esc(st.last_error)}</div>` : ''}
      <button onclick="m2mV2.provision()"
              class="px-4 py-2.5 bg-primary-600 text-white rounded-lg text-sm font-medium
                     hover:bg-primary-700">Create Service Account</button>`);
  }

  /* Step 3 — project + Databricks target + catalog. The three FR-06 sub-parts are rendered as
     one form: every field is needed before the connection can exist at all, because the
     connection id is the hash of (hub, project, workspace, catalog). */
  function renderTargetStep(body) {
    const f = S.form;
    const blocker = portalState.targetBlocker(f);
    const robot = (S.status || {}).robot || {};

    body.innerHTML = panel('Target', `
      <p class="text-sm text-slate-600 mb-4">
        Choose the ACC project and Databricks destination. One pipeline is allowed per catalog.
        The hub Service Account will be reused for this connection.
      </p>

      <label class="block text-sm font-medium mb-1" for="f-project">ACC Project</label>
      <select id="f-project" onchange="m2mV2.onProject(this)"
              class="w-full border border-slate-200 rounded-lg px-3 py-2.5 text-sm mb-2">
        <option value="">${S.projects ? 'Select a project…' : 'Loading projects…'}</option>
        ${(S.projects || []).map((p) => `
          <option value="${esc(p.id)}" ${f.project_id === p.id ? 'selected' : ''}>
            ${esc(p.name || p.id)}</option>`).join('')}
      </select>

      ${f.project_id ? (f.robot_verified
        ? notice('emerald', `✓ Robot verified on ACC project — ${esc(robot.email || '')}`)
        : `${notice('amber', `Robot not verified on this project yet.
             Invite <span class="font-mono">${esc(robot.email || 'the robot')}</span> to this
             project in ACC, then verify.`)}
           <div id="project-verify"></div>
           <button onclick="m2mV2.verifyProject()"
                   class="px-4 py-2.5 border border-slate-200 rounded-lg text-sm font-medium
                          hover:bg-slate-50 mb-4">Verify robot in ACC</button>`) : ''}

      <label class="block text-sm font-medium mb-1 mt-2" for="f-ws">
        Databricks Workspace URL</label>
      <input id="f-ws" type="url" value="${esc(f.workspace_url)}"
             oninput="m2mV2.onField('workspace_url', this.value)"
             placeholder="https://&lt;workspace-url&gt;.cloud.databricks.com"
             class="w-full border border-slate-200 rounded-lg px-3 py-2.5 text-sm mb-4 font-mono">

      <div class="border border-slate-200 rounded-lg p-4 mb-4">
        <div class="text-sm font-medium mb-1">Databricks Service Principal (optional)</div>
        <p class="text-xs text-slate-500 mb-3">
          Required only for scheduled sync. The secret is written to the vault — never stored
          or displayed in plaintext.
        </p>
        <input id="f-sp-id" type="text" value="${esc(f.dbx_client_id)}"
               oninput="m2mV2.onField('dbx_client_id', this.value)"
               placeholder="Service principal client id"
               class="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm mb-2 font-mono">
        <input id="f-sp-secret" type="password" autocomplete="new-password"
               oninput="m2mV2.onField('dbx_client_secret', this.value)"
               placeholder="Service principal secret"
               class="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm font-mono">
      </div>

      <div class="flex items-end gap-2 mb-4">
        <div class="flex-1">
          <label class="block text-sm font-medium mb-1" for="f-catalog">Unity Catalog</label>
          <select id="f-catalog" onchange="m2mV2.onField('catalog', this.value)"
                  class="w-full border border-slate-200 rounded-lg px-3 py-2.5 text-sm">
            <option value="">${S.catalogs ? 'Select a catalog…' : 'Authorize Databricks to list catalogs'}</option>
            ${(S.catalogs || []).map((c) => `
              <option value="${esc(c.name)}" ${f.catalog === c.name ? 'selected' : ''}
                      ${c.locked ? 'disabled' : ''}>
                ${esc(c.name)}${c.locked ? ' (in use)' : ''}</option>`).join('')}
          </select>
        </div>
        <button onclick="m2mV2.authorizeDatabricks()"
                class="px-4 py-2.5 border border-slate-200 rounded-lg text-sm font-medium
                       hover:bg-slate-50">🔒 Authorize Databricks</button>
        <button onclick="m2mV2.loadCatalogs()"
                class="px-4 py-2.5 border border-slate-200 rounded-lg text-sm font-medium
                       hover:bg-slate-50">↻ Refresh list</button>
      </div>

      ${blocker ? notice('amber', esc(portalState.TARGET_MESSAGES[blocker])) : ''}
      <button onclick="m2mV2.createConnection()" ${blocker ? 'disabled' : ''}
              class="px-4 py-2.5 rounded-lg text-sm font-medium ${blocker
                ? 'bg-slate-100 text-slate-400 cursor-not-allowed'
                : 'bg-primary-600 text-white hover:bg-primary-700'}">
        ▶ Run workspace provisioning</button>`);

    if (!S.projects) loadProjects();
  }

  function renderBootstrapStep(body) {
    body.innerHTML = panel('Bootstrap Progress', `
      ${notice('blue', 'Bootstrap running… this takes 1–2 minutes.')}
      <div id="bs-list" class="space-y-2 text-sm"></div>
      <div id="bs-done" class="mt-4"></div>`);
    pollBootstrap();
    startPolling(pollBootstrap, 2000);
  }

  function renderBootstrapList(progress) {
    const list = el('bs-list');
    if (!list) return;
    list.innerHTML = portalState.BOOTSTRAP_STEPS.map((label, i) => {
      const n = i + 1;
      const state = progress.error && n === progress.step ? 'error'
        : progress.step > n || progress.done && !progress.error ? 'done'
        : progress.step === n ? 'running' : 'todo';
      const mark = { done: '✓', running: '◌', error: '✕', todo: '·' }[state];
      const cls = { done: 'text-emerald-600', running: 'text-primary-600',
                    error: 'text-rose-600', todo: 'text-slate-400' }[state];
      return `<div class="flex items-center gap-2 ${cls}">
        <span class="w-4 text-center">${mark}</span>
        <span class="${state === 'todo' ? 'text-slate-400' : 'text-slate-700'}">${label}</span>
      </div>`;
    }).join('');
  }

  function renderSyncStep(body) {
    const d = S.connection;
    const run = d && d.latest_run;
    body.innerHTML = panel('Sync Data', `
      <p class="text-sm text-slate-600 mb-4">
        Push ACC data into Unity Catalog Bronze tables via the Data Connector.
      </p>
      ${notice('blue', `Sync Snapshot exports the snapshot-only service groups plus a
        <span class="font-mono">cdc*</span> baseline for Pipeline B, then runs both pipelines.
        At most once per 24 hours.`)}
      <div class="flex items-center gap-3 mb-4">
        <button onclick="m2mV2.startSync('snapshot')"
                class="px-4 py-2.5 bg-emerald-600 text-white rounded-lg text-sm font-medium
                       hover:bg-emerald-700">▶ Sync Snapshot</button>
        <button onclick="m2mV2.refreshSyncStep()"
                class="px-4 py-2.5 border border-slate-200 rounded-lg text-sm font-medium
                       hover:bg-slate-50">Refresh status</button>
      </div>
      <div id="run-card">${runCard(run)}</div>
      <div class="mt-6 text-right">
        <button onclick="m2mV2.finishSetup()"
                class="px-4 py-2.5 border border-slate-200 rounded-lg text-sm font-medium
                       hover:bg-slate-50">Go to Dashboard</button>
      </div>`);
    if (portalState.isRunInFlight(run)) startPolling(refreshSyncStep, 10000);
  }

  function renderHelp(target) {
    const cards = [
      ['Hub administrators only', 'Access is per hub via ACC. Project admins and viewers are blocked.'],
      ['One robot per hub', 'A single service account is reused; there is a 10-robot limit per Client ID.'],
      ['Whitelist is a hard gate', 'Custom Integration cannot be automated; setup is blocked until whitelisted.'],
      ['Headless by default', 'Robot plus service principal — no human session is used for scheduled sync.'],
    ];
    target.innerHTML = `<h1 class="text-2xl font-semibold mb-1">Help</h1>
      <p class="text-sm text-slate-600 mb-6">How the ACC → Databricks Connector works</p>
      <div class="grid sm:grid-cols-2 gap-4">${cards.map(([t, d]) => `
        <div class="bg-white rounded-xl border border-slate-200 p-6">
          <div class="font-semibold mb-1">${t}</div>
          <div class="text-sm text-slate-600">${d}</div>
        </div>`).join('')}</div>`;
  }

  // ── actions ─────────────────────────────────────────────────
  async function verifyWhitelist() {
    const out = el('verify-result');
    out.innerHTML = `<div class="text-sm text-slate-500 mb-3">Checking…</div>`;
    try {
      const r = await api(`/api/tenants/${hubPath()}/ssa/verify`, { method: 'POST' });
      S.status = r;
      if (r.passed) {
        toast('✓ Whitelist verified — probe passed');
        S.step = null;
        go('setup');
      } else {
        out.innerHTML = notice('rose', `<div class="font-medium mb-1">${esc(r.message)}</div>
          ${esc(r.remediation || '')}`);
      }
    } catch (err) {
      out.innerHTML = notice('rose', esc(err.message));
    }
  }

  async function provision() {
    if (S.busy) return;
    S.busy = true;
    try {
      S.status = await api(`/api/tenants/${hubPath()}/ssa/provision`, { method: 'POST' });
      toast('Service account ready');
    } catch (err) {
      toast(err.message);
      await refreshStatus();
    } finally {
      S.busy = false;
      go('setup');
    }
  }

  async function rotateKey() {
    if (!confirm('Rotate this hub\'s robot signing key? Running syncs re-mint automatically.')) {
      return;
    }
    try {
      S.status = await api(`/api/tenants/${hubPath()}/ssa/rotate-key`, { method: 'POST' });
      toast('Robot key rotated');
    } catch (err) {
      toast(err.message);
    }
    go(S.view);
  }

  function gotoStep(n) {
    S.step = n;
    go('setup');
  }

  function newConnection() {
    S.connection = null;
    S.catalogs = null;
    resetForm();
    S.step = portalState.setupStepFor(S.status && S.status.onboarding_status) < 3 ? null : 3;
    go('setup');
  }

  function onField(name, value) {
    S.form[name] = value;
    // Re-rendering on every keystroke would steal focus, so only the gate button is updated.
    const blocker = portalState.targetBlocker(S.form);
    const button = document.querySelector('[onclick="m2mV2.createConnection()"]');
    if (button) {
      button.disabled = !!blocker;
      button.className = 'px-4 py-2.5 rounded-lg text-sm font-medium ' + (blocker
        ? 'bg-slate-100 text-slate-400 cursor-not-allowed'
        : 'bg-primary-600 text-white hover:bg-primary-700');
    }
  }

  function onProject(select) {
    const option = select.options[select.selectedIndex];
    S.form.project_id = select.value;
    S.form.project_name = option ? option.textContent.trim() : '';
    // A new project has to be verified on its own — never inherit the previous answer (D-15).
    S.form.robot_verified = false;
    go('setup');
  }

  async function loadProjects() {
    try {
      const body = await api(`/projects?hub_id=${hubPath()}`);
      S.projects = body.projects || [];
    } catch (err) {
      S.projects = [];
      toast(err.message);
    }
    if (S.view === 'setup') go('setup');
  }

  async function verifyProject() {
    const out = el('project-verify');
    if (out) out.innerHTML = `<div class="text-sm text-slate-500 mb-3">Checking…</div>`;
    try {
      const r = await api(
        `/api/tenants/${hubPath()}/projects/${encodeURIComponent(S.form.project_id)}/verify`,
        { method: 'POST' },
      );
      S.form.robot_verified = !!r.passed;
      if (r.passed) toast('✓ Robot verified on this project');
      else if (out) {
        out.innerHTML = notice('rose', `<div class="font-medium mb-1">${esc(r.message)}</div>
          ${esc(r.remediation || '')}`);
        return;
      }
    } catch (err) {
      if (out) out.innerHTML = notice('rose', esc(err.message));
      return;
    }
    go('setup');
  }

  function authorizeDatabricks() {
    const url = (S.form.workspace_url || '').trim();
    if (!url) { toast('Enter your Databricks workspace URL first'); return; }
    // Full-page redirect: the OAuth round trip comes back to /portal?dbx=1, so the wizard step
    // and form are stashed for init() to restore. The typed service principal secret is
    // deliberately not persisted across it.
    stashResume();
    location.href = '/portal/databricks-login?workspace_url=' + encodeURIComponent(url);
  }

  async function loadCatalogs() {
    try {
      const body = await api('/databricks/catalogs');
      S.catalogs = body.catalogs || [];
      if (!S.catalogs.length) toast('No catalogs available in that workspace');
    } catch (err) {
      S.catalogs = null;
      toast(err.reason === 'reauth_required'
        ? 'Authorize Databricks to list catalogs' : err.message);
    }
    if (S.view === 'setup') go('setup');
  }

  async function createConnection() {
    if (S.busy) return;
    const f = S.form;
    if (portalState.targetBlocker(f)) {
      toast(portalState.TARGET_MESSAGES[portalState.targetBlocker(f)]);
      return;
    }
    S.busy = true;
    try {
      const body = await api('/api/connections', {
        method: 'POST',
        body: JSON.stringify({
          project_id: f.project_id, project_name: f.project_name,
          workspace_url: f.workspace_url, catalog: f.catalog,
          dbx_client_id: f.dbx_client_id || undefined,
          dbx_client_secret: f.dbx_client_secret || undefined,
        }),
      });
      S.connection = body;
      // The secret has reached the vault; drop the only client-side copy.
      S.form.dbx_client_secret = '';
      await startBootstrap();
    } catch (err) {
      toast(err.message);
      if (err.reason === 'catalog_taken') S.form.catalog = '';
      go('setup');
    } finally {
      S.busy = false;
    }
  }

  async function startBootstrap() {
    const cid = S.connection.connection.connection_id;
    try {
      await api(`/api/connections/${encodeURIComponent(cid)}/bootstrap`, { method: 'POST' });
      gotoStep(4);
    } catch (err) {
      toast(err.message);
      gotoStep(3);
    }
  }

  async function pollBootstrap() {
    const cid = S.connection && S.connection.connection.connection_id;
    if (!cid) { stopPolling(); return; }
    let progress;
    try {
      progress = await api(`/api/connections/${encodeURIComponent(cid)}/bootstrap/status`);
    } catch (err) {
      stopPolling();
      toast(err.message);
      return;
    }
    renderBootstrapList(progress);
    const doneBox = el('bs-done');
    if (!progress.done || !doneBox) return;

    stopPolling();
    if (progress.error) {
      doneBox.innerHTML = notice('rose', `<div class="font-medium mb-1">Bootstrap failed</div>
        <div class="font-mono text-xs break-all">${esc(progress.error)}</div>`) + `
        <button onclick="m2mV2.startBootstrap()"
                class="px-4 py-2.5 bg-primary-600 text-white rounded-lg text-sm font-medium
                       hover:bg-primary-700">Retry bootstrap</button>`;
      return;
    }
    doneBox.innerHTML = notice('emerald', 'Bootstrap complete — continue to Sync.') + `
      <button onclick="m2mV2.gotoStep(5)"
              class="px-4 py-2.5 bg-primary-600 text-white rounded-lg text-sm font-medium
                     hover:bg-primary-700">Continue →</button>`;
    await refreshConnection();
  }

  async function refreshConnection() {
    const cid = S.connection && S.connection.connection.connection_id;
    if (!cid) return;
    try {
      S.connection = await api(`/api/connections/${encodeURIComponent(cid)}`);
    } catch (err) {
      toast(err.message);
    }
  }

  async function openConnection(connectionId) {
    try {
      S.connection = await api(`/api/connections/${encodeURIComponent(connectionId)}`);
    } catch (err) {
      toast(err.message);
      return;
    }
    go('detail');
  }

  async function refreshDetail() {
    await refreshConnection();
    if (S.view === 'detail') go('detail');
  }

  async function refreshSyncStep() {
    await refreshConnection();
    if (S.view === 'setup') go('setup');
  }

  async function startSync(mode) {
    const cid = S.connection && S.connection.connection.connection_id;
    if (!cid) return;
    const path = mode === 'cdc' ? '/sync/cdc' : '/sync';
    try {
      await api(`/api/connections/${encodeURIComponent(cid)}${path}`, { method: 'POST' });
      toast(`${mode === 'cdc' ? 'CDC' : 'Snapshot'} sync started`);
    } catch (err) {
      toast(err.message);
    }
    await refreshConnection();
    go(S.view);
  }

  async function toggleSchedule(enabled) {
    const cid = S.connection && S.connection.connection.connection_id;
    if (!cid) return;
    try {
      S.connection = await api(`/api/connections/${encodeURIComponent(cid)}/schedule`, {
        method: 'PUT', body: JSON.stringify({ enabled }),
      });
      toast(enabled ? 'Scheduled sync enabled' : 'Scheduled sync disabled');
    } catch (err) {
      // The server names the one missing thing (D-18) — show that, not a generic failure.
      toast(err.body && err.body.remediation ? err.body.remediation : err.message);
    }
    go('detail');
  }

  function showSpForm() {
    const box = el('sp-form');
    if (!box) return;
    box.classList.remove('hidden');
    box.innerHTML = `
      <input id="sp-id" type="text" placeholder="Service principal client id"
             class="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm mb-2 font-mono">
      <input id="sp-secret" type="password" autocomplete="new-password"
             placeholder="Service principal secret"
             class="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm mb-3 font-mono">
      <button onclick="m2mV2.saveSp()"
              class="px-4 py-2.5 bg-primary-600 text-white rounded-lg text-sm font-medium
                     hover:bg-primary-700">Save to vault</button>`;
  }

  async function saveSp() {
    const cid = S.connection && S.connection.connection.connection_id;
    const clientId = el('sp-id').value.trim();
    const secret = el('sp-secret').value;
    if (!cid || !clientId || !secret) { toast('Both the client id and secret are required'); return; }
    try {
      S.connection = await api(
        `/api/connections/${encodeURIComponent(cid)}/dbx-credentials`,
        { method: 'POST', body: JSON.stringify({ client_id: clientId, client_secret: secret }) },
      );
      toast('Service principal stored in vault');
    } catch (err) {
      toast(err.message);
    }
    go('detail');
  }

  async function finishSetup() {
    S.step = null;
    await Promise.all([refreshStatus(), refreshConnections()]);
    go('dashboard');
  }

  async function signOut() {
    stopPolling();
    await fetch('/api/session/logout', { method: 'POST' });
    location.href = '/portal';
  }

  function copy(text) {
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => toast('Copied'));
  }

  window.m2mV2 = {
    init, go, selectHub, showHubPicker, verifyWhitelist, provision, signOut, copy,
    gotoStep, newConnection, onField, onProject, verifyProject, authorizeDatabricks,
    loadCatalogs, createConnection, startBootstrap, openConnection, refreshDetail,
    refreshSyncStep, startSync, toggleSchedule, showSpForm, saveSp, finishSetup, rotateKey,
  };
  document.addEventListener('DOMContentLoaded', init);
})();
