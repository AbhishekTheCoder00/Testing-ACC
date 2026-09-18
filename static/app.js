// ─────────────────────────────────────────────
// State
// ─────────────────────────────────────────────
let _accConnected    = false;  // OAuth token present
let _accConfigSaved  = false;  // hub/project saved
let _dbxBootstrapped = false;  // bootstrap complete
let _dbxTokenPresent = false;  // Databricks OAuth token saved
let _dbxWorkspaceUrl = '';
let _hasSyncHistory  = false;  // at least one completed sync
let _activeStep      = 1;
let _dashChart           = null;
let _dashChartOutcomes   = null;
let _dashAdvancedOpen    = false;
let _editingProject = false;
let _syncClickPending = null;  // 'snapshot' | 'cdc' while waiting for server active run

// ─────────────────────────────────────────────
// Sidebar navigation
// ─────────────────────────────────────────────
function _setContentWide(wide) {
  const c = document.querySelector('.content');
  if (c) c.classList.toggle('content-wide', !!wide);
}

function navTo(step) {
  if (step === 2 && !_accConfigSaved) return;
  if (step === 3 && !_dbxBootstrapped) return;
  if (step === 4 && !_hasSyncHistory) return;
  if (step === 5 && !_hasSyncHistory) return;

  _activeStep = step;
  _setContentWide(step === 5);
  [1, 2, 3, 4, 5].forEach(i => {
    const n = document.getElementById(`nav-${i}`);
    if (n) n.classList.remove('active');
  });
  document.getElementById(`nav-${step}`).classList.add('active');

  // Hide all panels
  ['1a', '1b', '1c', '2', '3', '4', '5'].forEach(id => {
    const p = document.getElementById(`panel-${id}`);
    if (p) p.classList.remove('active');
  });

  if (step === 1) {
    if (_editingProject) {
        document.getElementById("acc-review-buttons").style.display = "none";
    } else {
        document.getElementById("acc-review-buttons").style.display = "flex";
    }
    if (_accConfigSaved)       showPanel('1c');
    else if (_accConnected) {
      showPanel('1b');
      // Populate hub dropdown whenever panel-1b becomes visible
      const hs = document.getElementById('hub-select');
      if (hs && hs.options.length <= 1) loadHubs();
    }
    else                       showPanel('1a');
  } else if (step === 2) {
    showPanel('2');
    const locked = document.getElementById('panel-2-locked');
    const form   = document.getElementById('panel-2-form');
    locked.style.display = 'none';
    form.style.display   = 'block';
    if (_dbxTokenPresent && !_dbxBootstrapped) {
      showDbxPostLogin(_dbxWorkspaceUrl);
    }
  } else if (step === 3) {
    showPanel('3');
    const locked = document.getElementById('panel-3-locked');
    const form   = document.getElementById('panel-3-form');
    locked.style.display = 'none';
    form.style.display   = 'block';
    checkSyncStatus();
    loadRecentRuns('sync-recent-runs', 'snapshot');
  } else if (step === 4) {
    showPanel('4');
    const locked = document.getElementById('panel-4-locked');
    const form   = document.getElementById('panel-4-form');
    if (_hasSyncHistory) {
      locked.style.display = 'none';
      form.style.display   = 'block';
      loadAutoCdcSection();
    } else {
      locked.style.display = 'flex';
      form.style.display   = 'none';
    }
  } else if (step === 5) {
    showPanel('5');
    loadDashboard();
  }
}

function showPanel(id) {
  document.getElementById(`panel-${id}`).classList.add('active');
}

// ─────────────────────────────────────────────
// Sidebar item state helpers
// ─────────────────────────────────────────────
function setNavDone(step) {
  const n  = document.getElementById(`nav-${step}`);
  const nc = document.getElementById(`nc-${step}`);
  const lk = document.getElementById(`lock-${step}`);
  n.classList.remove('locked', 'active');
  n.classList.add('done');
  nc.textContent = '✓';
  if (lk) lk.style.display = 'none';
}

function unlockNav(step) {
  const n  = document.getElementById(`nav-${step}`);
  const lk = document.getElementById(`lock-${step}`);
  n.classList.remove('locked');
  if (lk) lk.style.display = 'none';
}

// ─────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────
function msg(id, text, type='info') {
  document.getElementById(id).innerHTML = `<div class="msg msg-${type}">${text}</div>`;
}

function openStartOverModal() {
  const modal = document.getElementById('start-over-modal');
  if (!modal) return;
  modal.hidden = false;
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
  const cancelBtn = document.getElementById('start-over-cancel');
  if (cancelBtn) cancelBtn.focus();
}

function closeStartOverModal() {
  const modal = document.getElementById('start-over-modal');
  if (!modal) return;
  modal.hidden = true;
  modal.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('modal-open');
}

function confirmStartOver(e) {
  if (e) e.preventDefault();
  openStartOverModal();
  return false;
}

function proceedStartOver() {
  window.location.href = '/reset';
}

function _initStartOverModal() {
  const modal = document.getElementById('start-over-modal');
  if (!modal) return;

  document.getElementById('start-over-cancel')?.addEventListener('click', closeStartOverModal);
  document.getElementById('start-over-confirm')?.addEventListener('click', proceedStartOver);

  modal.addEventListener('click', (ev) => {
    if (ev.target === modal) closeStartOverModal();
  });

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !modal.hidden) closeStartOverModal();
  });
}

// ─────────────────────────────────────────────
// Step 1 — ACC OAuth
// ─────────────────────────────────────────────
function connectACC() {
  window.location.href = '/connect/acc';
}

async function loadHubs() {
  const hs = document.getElementById('hub-select');
  const ps = document.getElementById('project-select');
  hs.innerHTML = '<option value="">-- Loading hubs... --</option>';
  hs.disabled = true;
  ps.innerHTML = '<option value="">-- Select Hub first --</option>';
  ps.disabled = true;
  document.getElementById('folder-select').disabled = true;

  const resp = await fetch('/hubs');
  const data = await resp.json();
  hs.disabled = false;
  hs.innerHTML = '<option value="">-- Select Hub --</option>';

  if (!resp.ok) {
    msg('acc-config-msg',
      `Failed to load hubs: ${data.error || resp.status}.`, 'error');
    return;
  }
  if (!data.hubs || !data.hubs.length) {
    msg('acc-config-msg',
      'No hubs found. Follow the Custom Integration steps above, wait a few minutes, then '
      + '<a href="#" onclick="loadHubs(); return false;">retry loading hubs</a>.', 'error');
    return;
  }
  data.hubs.forEach(h => {
    const opt = document.createElement('option');
    opt.value = h.id;
    opt.textContent = h.name;
    opt.dataset.name = h.name;
    hs.appendChild(opt);
  });
  msg('acc-config-msg', `${data.hubs.length} hub(s) found. Select one to load its projects.`, 'info');
}

async function loadProjects() {
  const hs = document.getElementById('hub-select');
  const ps = document.getElementById('project-select');
  const hubId = hs.value;
  if (!hubId) return;

  ps.innerHTML = '<option value="">-- Loading projects... --</option>';
  ps.disabled = true;
  document.getElementById('folder-select').disabled = true;

  const resp = await fetch(`/projects?hub_id=${encodeURIComponent(hubId)}`);
  const data = await resp.json();
  ps.disabled = false;
  ps.innerHTML = '<option value="">-- Select Project --</option>';

  if (!resp.ok) {
    msg('acc-config-msg', `Failed to load projects: ${data.error || resp.status}.`, 'error');
    return;
  }
  if (!data.projects || !data.projects.length) {
    msg('acc-config-msg', 'No projects found in this hub.', 'error');
    return;
  }

  const hubOpt = hs.options[hs.selectedIndex];
  data.projects.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.name;
    opt.dataset.name    = p.name;
    opt.dataset.hubId   = hubId;
    opt.dataset.hubName = hubOpt?.dataset.name || hubOpt?.textContent || '';
    ps.appendChild(opt);
  });
  msg('acc-config-msg', `${data.projects.length} project(s) found. Select one to continue.`, 'info');
}

async function loadFolders() {
  const projSel   = document.getElementById('project-select');
  const projectId = projSel.value;
  const opt       = projSel.options[projSel.selectedIndex];
  const hubId     = opt?.dataset.hubId || '';
  const fs        = document.getElementById('folder-select');
  fs.innerHTML = '<option value="">-- Select Folder (optional) --</option>';
  fs.disabled = true;
  if (!hubId || !projectId) return;
  const resp = await fetch(`/folders?hub_id=${encodeURIComponent(hubId)}&project_id=${encodeURIComponent(projectId)}`);
  const data = await resp.json();
  fs.disabled = false;
  (data.folders || []).forEach(f => {
    const opt = document.createElement('option');
    opt.value = f.id; opt.textContent = f.name; opt.dataset.name = f.name;
    fs.appendChild(opt);
  });
}

function changeProject() {
  _accConfigSaved = false;
  document.getElementById('nav-1').classList.remove('done');
  document.getElementById('nav-1').classList.add('active');
  document.getElementById('nc-1').textContent = '1';
  document.getElementById("acc-review-buttons").style.display = "none";
  showPanel('1b');
  loadHubs();
}

async function saveACCConfig() {
  const hubSel    = document.getElementById('hub-select');
  const projSel   = document.getElementById('project-select');
  const folderSel = document.getElementById('folder-select');
  const selOpt    = projSel.options[projSel.selectedIndex];
  const rawHubId  = selOpt?.dataset.hubId || '';
  // Strip 'b.' prefix to get bare account UUID for Data Connector API
  const accountId = rawHubId.startsWith('b.') ? rawHubId.slice(2) : rawHubId;
  const body = {
    hub_id:         rawHubId,
    hub_name:       selOpt?.dataset.hubName || '',
    project_id:     projSel.value,
    project_name:   selOpt?.dataset.name    || '',
    folder_id:      folderSel.value || null,
    folder_name:    folderSel.options[folderSel.selectedIndex]?.dataset.name || null,
    acc_account_id: accountId,
  };
  if (!body.hub_id || !body.project_id) {
    msg('acc-config-msg', 'Please select a Hub and Project.', 'error'); return;
  }
  const resp = await fetch('/save-acc-config', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  const data = await resp.json();
  if (!resp.ok) { msg('acc-config-msg', data.error, 'error'); return; }

  _accConfigSaved = true;
  setNavDone(1);
  unlockNav(2);

  // Show done detail
  document.getElementById('acc-done-detail').innerHTML =
    `<strong>Hub:</strong> ${body.hub_name}<br>
     <strong>Project:</strong> ${body.project_name}<br>
     ${body.folder_name ? `<strong>Folder:</strong> ${body.folder_name}` : ''}`;
  document.getElementById('acc-done-pill').className = 'status-pill pill-connected';
  document.getElementById('acc-done-pill').innerHTML  = `&#10003; Connected &mdash; ${body.project_name}`;

  // Auto-navigate to Step 2
  setTimeout(() => navTo(2), 600);
}

// ─────────────────────────────────────────────
// Step 2 — Databricks + Bootstrap
// ─────────────────────────────────────────────
let _bsPollTimer = null;

function detectCloudProvider(url) {
  if (!url) return null;
  // Azure-specific detection intentionally disabled.
  // if (url.includes('azuredatabricks.net'))           return 'azure';
  if (url.includes('azuredatabricks.net'))           return 'aws';
  if (url.includes('cloud.databricks.com') ||
      url.includes('databricks.us'))                 return 'aws';
  if (url.includes('gcp.databricks.com'))            return 'gcp';
  return null;
}

function onWorkspaceUrlInput() {
  _editingProject = false;
  document.getElementById("acc-review-buttons").style.display = "flex";
  const url    = document.getElementById('workspace-url-input').value.trim();
  const cloud  = detectCloudProvider(url);
  const badge  = document.getElementById('cloud-badge');
  badge.className = 'cloud-badge';
  // Azure-specific badge path intentionally disabled.
  // if (cloud === 'azure') {
  //   badge.className += ' cloud-azure';
  //   badge.textContent = '🔷 Azure Databricks detected';
  //   badge.style.display = 'inline-block';
  //   btn.style.display = 'inline-flex';
  // } else
  if (cloud === 'aws') {
    badge.className += ' cloud-aws';
    badge.textContent = '🟠 AWS Databricks detected';
    badge.style.display = 'inline-block';
  } else if (cloud === 'gcp') {
    badge.className += ' cloud-gcp';
    badge.textContent = '🟢 GCP Databricks detected';
    badge.style.display = 'inline-block';
  } else if (url.length > 10) {
    badge.className += ' cloud-unknown';
    badge.textContent = '⚠ Unknown workspace URL — check format';
    badge.style.display = 'inline-block';
  } else {
    badge.style.display = 'none';
  }
}

function _dbxPayload() {
  return {
    workspace_url: document.getElementById('workspace-url-input').value.trim(),
    client_id: document.getElementById('dbx-client-id-input').value.trim(),
    client_secret: document.getElementById('dbx-client-secret-input').value.trim(),
  };
}

function _dbxValidate(payload) {
  if (!payload.workspace_url) { msg('dbx-msg', 'Please enter your Databricks Workspace URL.', 'error'); return false; }
  if (!payload.client_id) { msg('dbx-msg', 'Please enter your Databricks Client ID.', 'error'); return false; }
  if (!payload.client_secret) { msg('dbx-msg', 'Please enter your Databricks Client Secret.', 'error'); return false; }
  return true;
}

function _dbxAuthStatus(message, kind) {
  const el = document.getElementById('dbx-auth-status');
  if (!el) return;
  el.style.display = 'block';
  el.className = 'dbx-auth-status ' + (kind || '');
  el.textContent = message || '';
}

async function _dbxSaveCredentials(payload) {
  const resp = await fetch('/connect/databricks/save-credentials', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (!resp.ok || !data.ok) {
    throw new Error(data.error || 'Failed to save credentials.');
  }
}

function authorizeDatabricks() {
  const payload = _dbxPayload();
  if (!_dbxValidate(payload)) return;
  const btn = document.getElementById('btn-dbx-authorize');
  if (btn) btn.disabled = true;
  _dbxAuthStatus('Saving credentials, then redirecting to Databricks&hellip;', 'info');
  msg('dbx-msg', '', '');
  (async () => {
    try {
      await _dbxSaveCredentials(payload);
      window.location.href = '/connect/databricks-oauth?workspace_url=' + encodeURIComponent(payload.workspace_url);
    } catch (e) {
      _dbxAuthStatus(e.message || 'Failed to authorize.', 'error');
      if (btn) btn.disabled = false;
    }
  })();
}

async function loadDbxCredsStatus() {
  const form = document.getElementById('dbx-creds-form');
  const saved = document.getElementById('dbx-creds-saved');
  if (!form || !saved) return;
  try {
    const resp = await fetch('/connect/databricks/status');
    const data = await resp.json();
    if (data.redirect_uri) {
      const cb = document.getElementById('dbx-callback-url-text');
      if (cb) cb.textContent = data.redirect_uri;
    }
    if (data.saved) {
      showDbxCredsSaved(data.workspace_url || '');
      if (data.workspace_url) {
        const inp = document.getElementById('workspace-url-input');
        if (inp && !inp.value.trim()) { inp.value = data.workspace_url; }
      }
    }
  } catch (e) { /* non-critical */ }
}

function copyDbxCallbackUrl() {
  const el = document.getElementById('dbx-callback-url-text');
  const text = el ? el.textContent : '';
  if (!text) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => flashCopyBtn());
  } else {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); flashCopyBtn(); } catch (e) {}
    document.body.removeChild(ta);
  }
}

function flashCopyBtn() {
  const btn = document.querySelector('.btn-copy');
  if (!btn) return;
  const orig = btn.textContent;
  btn.textContent = 'Copied!';
  setTimeout(() => { btn.textContent = orig; }, 1500);
}

function showDbxCredsSaved(url) {
  const form = document.getElementById('dbx-creds-form');
  const saved = document.getElementById('dbx-creds-saved');
  if (form) form.style.display = 'none';
  if (saved) saved.style.display = 'block';
  const inp = document.getElementById('workspace-url-input');
  if (inp && url && !inp.value.trim()) { inp.value = url; }
  const lbl = document.getElementById('dbx-saved-workspace');
  if (lbl) lbl.textContent = url ? ('Workspace: ' + url) : '';
  if (inp) onWorkspaceUrlInput();
}

function showDbxPostLogin(workspaceUrl) {
  document.getElementById('dbx-login-wrap').style.display = 'none';
  document.getElementById('dbx-post-login').style.display = 'block';
  const card = document.getElementById('dbx-workspace-card');
  const urlEl = document.getElementById('dbx-workspace-url');
  const openBtn = document.getElementById('btn-open-dbx');
  const cleanUrl = (workspaceUrl || '').replace(/\/+$/, '');
  if (urlEl) urlEl.textContent = workspaceUrl || '';
  if (openBtn) openBtn.setAttribute('href', cleanUrl || '#');
  if (card) card.style.display = workspaceUrl ? 'flex' : 'none';
  document.getElementById('dbx-status-pill').className = 'status-pill pill-connected';
  document.getElementById('dbx-status-pill').innerHTML = '&#10003; Databricks authorized &mdash; choose a catalog';
}

function copyDbxWorkspaceUrl() {
  const el = document.getElementById('dbx-workspace-url');
  const text = el ? (el.textContent || '').trim() : '';
  if (!text) return;
  const done = () => flashDbxCopyBtn();
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => { try { document.execCommand('copy'); } catch (e) {} done(); });
  } else {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) {}
    document.body.removeChild(ta);
  }
}

function flashDbxCopyBtn() {
  const btn = document.getElementById('btn-copy-dbx');
  if (!btn) return;
  const orig = btn.textContent;
  btn.textContent = 'Copied!';
  setTimeout(() => { btn.textContent = orig; }, 1500);
}

function showDbxCredsForm(showClear) {
  const form = document.getElementById('dbx-creds-form');
  const saved = document.getElementById('dbx-creds-saved');
  if (!form || !saved) return;
  if (showClear) { form.style.display = 'block'; saved.style.display = 'none'; }
  else { form.style.display = 'none'; saved.style.display = 'block'; }
}

function reauthorizeDatabricks() {
  const workspaceUrl = (document.getElementById('dbx-saved-workspace') || {}).textContent || '';
  const match = workspaceUrl.replace(/^Workspace:\s*/, '').trim();
  if (!match) {
    showDbxCredsForm(true);
    return;
  }
  const inp = document.getElementById('workspace-url-input');
  if (inp && !inp.value.trim()) inp.value = match;
  const btn = document.getElementById('btn-dbx-reauthorize');
  if (btn) btn.disabled = true;
  _dbxAuthStatus('Redirecting to Databricks authorization&hellip;', 'info');
  msg('dbx-msg', '', '');
  window.location.href = '/connect/databricks-oauth?workspace_url=' + encodeURIComponent(match);
}

function showDbxReauthNeeded(message) {
  _dbxTokenPresent = false;
  document.getElementById('dbx-login-wrap').style.display = 'block';
  document.getElementById('dbx-post-login').style.display = 'none';
  document.getElementById('dbx-status-pill').className = 'status-pill pill-disconnected';
  document.getElementById('dbx-status-pill').innerHTML = '&#9679; Session expired — sign in again';
  if (_dbxWorkspaceUrl) {
    const inp = document.getElementById('workspace-url-input');
    if (inp) { inp.value = _dbxWorkspaceUrl; onWorkspaceUrlInput(); }
  }
  msg('dbx-msg', message || 'Databricks session expired. Sign in again.', 'error');
}

// Cache the enriched catalog list so onUcCatalogSelected can render details
// without an extra round trip.
let _ucCatalogsCache = [];

async function loadUcCatalogs() {
  const sel = document.getElementById('uc-catalog-select');
  const btnRef = document.getElementById('btn-refresh-catalogs');
  const detail = document.getElementById('uc-catalog-detail');
  if (btnRef) btnRef.disabled = true;
  sel.innerHTML = '<option value="">-- Loading catalogs --</option>';
  sel.disabled = true;
  if (detail) { detail.style.display = 'none'; detail.innerHTML = ''; }
  try {
    const resp = await fetch('/databricks/catalogs');
    const data = await resp.json();
    if (!resp.ok) {
      if (data.reauth_required) {
        showDbxReauthNeeded(data.error);
      } else {
        msg('dbx-msg', data.error || ('Failed to list catalogs (' + resp.status + ')'), 'error');
      }
      sel.innerHTML = '<option value="">-- Error --</option>';
      sel.disabled = false;
      if (btnRef) btnRef.disabled = false;
      return;
    }
    const rows = data.catalogs || [];
    _ucCatalogsCache = rows;

    sel.innerHTML = '<option value="">-- Select a catalog --</option>';
    rows.forEach(c => {
      const o = document.createElement('option');
      o.value = c.name;
      // A catalog we cannot use stays visible but unselectable — hiding it
      // would make the list look wrong to the user who can see the catalog
      // in Databricks. The suffix says why it is not selectable.
      let suffix = '';
      if (c.status === 'locked') suffix = '  — in use by another user';
      else if (c.status === 'owned') suffix = '  — provisioned by you';
      else if (c.status === 'no_permission') suffix = '  — no write access';
      o.textContent = `${c.name}  (${c.storage_label})${suffix}`;
      o.disabled = !!c.locked;
      sel.appendChild(o);
    });
    sel.disabled = false;
    onUcCatalogSelected();

    const lockedCount = rows.filter(c => c.status === 'locked').length;
    const noAccessCount = rows.filter(c => c.status === 'no_permission').length;
    if (rows.length === 0) {
      msg('dbx-msg', 'No catalogs found. Create a Unity Catalog in Databricks, then refresh.', 'error');
    } else if (lockedCount || noAccessCount) {
      const reasons = [];
      if (lockedCount) reasons.push(`${lockedCount} provisioned by another user`);
      if (noAccessCount) reasons.push(`${noAccessCount} without write access`);
      msg('dbx-msg',
        `${rows.length} catalog(s) found; ${reasons.join(', ')}. Pick a selectable one, then run provisioning.`,
        'info');
    } else {
      msg('dbx-msg',
        `${rows.length} catalog(s) found. Pick one, then run provisioning.`,
        'info');
    }
  } catch (e) {
    msg('dbx-msg', 'Error loading catalogs: ' + e.message, 'error');
    sel.innerHTML = '<option value="">-- Error --</option>';
    sel.disabled = false;
  }
  if (btnRef) btnRef.disabled = false;
}

function onUcCatalogSelected() {
  const sel = document.getElementById('uc-catalog-select');
  const detail = document.getElementById('uc-catalog-detail');
  const startBtn = document.getElementById('btn-start-provisioning');
  const selected = (_ucCatalogsCache || []).find(c => c.name === sel.value);
  if (startBtn) startBtn.disabled = !sel.value || !!(selected && selected.locked);
  if (!detail) return;

  if (!sel.value) {
    detail.style.display = 'none';
    detail.innerHTML = '';
    return;
  }
  const cat = selected;
  if (!cat) { detail.style.display = 'none'; return; }

  // Each catalog carries its own pipelines, so provisioning state is a
  // property of the catalog, not of the user's session.
  let ownershipLine = '';
  if (cat.status === 'locked') {
    ownershipLine = `<div style="font-size:12px;color:#b45309;margin-top:4px"><strong>In use by another user.</strong> Its pipelines belong to whoever provisioned it — pick a different catalog.</div>`;
  } else if (cat.status === 'no_permission') {
    ownershipLine = `<div style="font-size:12px;color:#b45309;margin-top:4px"><strong>No write access.</strong> Provisioning creates <code>${_escapeHtml(cat.name)}.bronze</code>, so you need <code>CREATE SCHEMA</code> on this catalog. Ask your Databricks admin to run:<br><code>GRANT CREATE SCHEMA, USE CATALOG ON CATALOG \`${_escapeHtml(cat.name)}\` TO \`you@example.com\`;</code></div>`;
  } else if (cat.status === 'owned') {
    const pipeLine = cat.snapshot_pipeline_name
      ? `<div style="font-size:12px;color:#475569;margin-top:2px">Pipelines: <code>${_escapeHtml(cat.snapshot_pipeline_name)}</code>, <code>${_escapeHtml(cat.cdc_pipeline_name || '')}</code></div>`
      : '';
    ownershipLine = `<div style="font-size:12px;color:#15803d;margin-top:4px"><strong>Provisioned by you.</strong></div>${pipeLine}`;
  } else {
    ownershipLine = `<div style="font-size:12px;color:#475569;margin-top:4px">Available — no pipelines yet.</div>`;
  }

  const storageLine = cat.storage_root
    ? `<div style="font-size:12px;color:#475569;margin-top:4px"><strong>Storage root:</strong> <code>${_escapeHtml(cat.storage_root)}</code></div>`
    : `<div style="font-size:12px;color:#475569;margin-top:4px">No explicit <code>storage_root</code> \u2014 Default Storage / system / foreign catalog.</div>`;
  const typeLine = cat.catalog_type
    ? `<div style="font-size:12px;color:#475569;margin-top:2px"><strong>Type:</strong> ${_escapeHtml(cat.catalog_type)}</div>`
    : '';
  const commentLine = cat.comment
    ? `<div style="font-size:12px;color:#475569;margin-top:2px"><em>${_escapeHtml(cat.comment)}</em></div>`
    : '';

  detail.style.display = 'block';
  detail.innerHTML = `
    <div style="padding:10px 12px;border:1px solid #cbd5e1;background:#f8fafc;border-radius:8px">
      <div style="font-size:13px;font-weight:600;color:#1a1a2e">${_escapeHtml(cat.storage_label || '')}</div>
      ${ownershipLine}
      ${typeLine}
      ${storageLine}
      ${commentLine}
    </div>`;
}

async function startProvisioning() {
  if (_bsPollTimer) {
    msg('dbx-msg', 'Bootstrap already running.', 'error');
    return;
  }
  const sel = document.getElementById('uc-catalog-select');
  const cat = sel.value;
  if (!cat) { msg('dbx-msg', 'Select a Unity Catalog first.', 'error'); return; }
  const meta = (_ucCatalogsCache || []).find(c => c.name === cat);
  if (meta && meta.status === 'no_permission') {
    msg('dbx-msg', 'You do not have CREATE SCHEMA on that catalog. Ask your Databricks admin to grant it, or pick another catalog.', 'error');
    return;
  }
  if (meta && meta.locked) {
    msg('dbx-msg', 'That catalog is already provisioned by another user.', 'error');
    return;
  }
  document.getElementById('btn-start-provisioning').disabled = true;
  document.getElementById('btn-refresh-catalogs').disabled = true;
  const resp = await fetch('/bootstrap/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ catalog_name: cat }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    msg('dbx-msg', data.error || 'Failed to start provisioning', 'error');
    document.getElementById('btn-start-provisioning').disabled = false;
    document.getElementById('btn-refresh-catalogs').disabled = false;
    return;
  }
  startBootstrapPolling();
}

function startBootstrapPolling() {
  document.getElementById('dbx-login-wrap').style.display = 'none';
  const post = document.getElementById('dbx-post-login');
  if (post) post.style.display = 'none';
  document.getElementById('bootstrap-progress').style.display = 'block';
  msg('dbx-msg', 'Bootstrap running&hellip; this takes 1&ndash;2 minutes.', 'info');
  _bsPollTimer = setInterval(pollBootstrap, 2000);
}

async function pollBootstrap() {
  const resp = await fetch('/bootstrap/status');
  const data = await resp.json();
  const step = data.step || 0;

  for (let i = 1; i <= 10; i++) {
    const dot = document.getElementById(`bs-dot-${i}`);
    const lbl = document.getElementById(`bs-msg-${i}`);
    if (i < step)        dot.className = 'bs-dot done';
    else if (i === step) { dot.className = 'bs-dot active'; lbl.textContent = data.message || lbl.textContent; }
    else                 dot.className = 'bs-dot';
  }

  if (data.error) {
    clearInterval(_bsPollTimer);
    _bsPollTimer = null;
    const dot = document.getElementById(`bs-dot-${step}`);
    if (dot) dot.className = 'bs-dot error';
    msg('dbx-msg', `Bootstrap failed at step ${step}: ${data.error}`, 'error');
    const bsBtn = document.getElementById('btn-start-provisioning');
    const rfBtn = document.getElementById('btn-refresh-catalogs');
    if (bsBtn) bsBtn.disabled = false;
    if (rfBtn) rfBtn.disabled = false;
    const post = document.getElementById('dbx-post-login');
    if (post) post.style.display = 'block';
    document.getElementById('bootstrap-progress').style.display = 'none';
    return;
  }

  if (data.done) {
    clearInterval(_bsPollTimer);
    _bsPollTimer = null;
    for (let i = 1; i <= 10; i++) document.getElementById(`bs-dot-${i}`).className = 'bs-dot done';
    _dbxBootstrapped = true;
    setNavDone(2);
    unlockNav(3);
    document.getElementById('dbx-status-pill').className = 'status-pill pill-connected';
    document.getElementById('dbx-status-pill').innerHTML = '&#10003; Databricks connected &mdash; Bootstrap complete';
    msg('dbx-msg', '&#10003; Bootstrap complete! Navigating to Sync&hellip;', 'success');
    // Auto-navigate to Step 3
    setTimeout(() => navTo(3), 1200);
  }
}

// ─────────────────────────────────────────────
// Step 3 — Sync
// ─────────────────────────────────────────────

function _escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

function _isRunTerminal(state) {
  return ['complete', 'failed', 'partial'].includes(state);
}

function _isRunActive(run) {
  return !!(run && run.state && !_isRunTerminal(run.state));
}

function _runDisplayNumber(run) {
  if (!run) return '—';
  const seq = run.user_run_seq;
  return seq != null && seq !== '' ? seq : run.run_id;
}

function _cdcTriggerLabel(triggerType) {
  const lo = String(triggerType || '').toLowerCase();
  return lo === 'auto' ? 'Daily CDC' : 'Manual CDC';
}

function _renderSyncRunDisplay(run, hubName, projName, groupLabel, triggerLabel) {
  if (!run) return '';

  const cls = run.state === 'complete' ? 'state-complete'
            : run.state === 'failed'   ? 'state-failed'
            : run.state === 'pending'  ? 'state-pending'
            : 'state-running';

  let counts = {};
  try { counts = JSON.parse(run.record_counts || '{}'); } catch (_) {}

  const breadcrumb = (hubName || projName)
    ? `<div style="font-size:12px;color:#666;margin:10px 0 14px;">
         <strong>Hub:</strong> ${_escapeHtml(hubName)}
         ${projName ? ` &rsaquo; <strong>Project:</strong> ${_escapeHtml(projName)}` : ''}
       </div>`
    : '';

  const fileCount = counts.files ?? null;
  const cardsHtml = fileCount !== null
    ? `<div class="sync-card">
         <div class="count">${fileCount}</div>
         <div class="label">CSV files uploaded</div>
         <div style="font-size:11px;color:#aaa;margin-top:2px;">${_escapeHtml(groupLabel)}</div>
       </div>`
    : (run.state === 'complete' ? '<p style="color:#aaa;font-size:13px">Sync complete.</p>'
                                : '<p style="color:#aaa;font-size:13px">Sync in progress&hellip;</p>');

  const triggerSuffix = triggerLabel
    ? ` &bull; ${_escapeHtml(triggerLabel)}`
    : '';

  return `
    <hr class="divider">
    <span class="state-tag ${cls}">${run.state.toUpperCase()}</span>
    ${breadcrumb}
    <div class="sync-grid" style="margin-top:4px">${cardsHtml}</div>
    <p style="margin-top:10px;font-size:12px;color:#999">
      Run #${_runDisplayNumber(run)} &bull; ${new Date(run.started_at * 1000).toLocaleString()}${triggerSuffix}
      ${run.error ? `<br><span style="color:#c0392b">${_escapeHtml(run.error)}</span>` : ''}
    </p>`;
}

async function _postSync(url, msgEl, label) {
  msg(msgEl, `Starting ${label}&hellip;`, 'info');
  const resp = await fetch(url, { method: 'POST' });
  const data = await resp.json();
  if (!resp.ok) {
    msg(msgEl, data.error || `Failed to start ${label}`, 'error');
    return false;
  }
  msg(msgEl,
      `&#10003; ${label} started. Polling every 10s&hellip; (Data Connector jobs can take 5&ndash;20 min)`,
      'info');
  return true;
}

/** Disable sync buttons immediately on click (before the POST round-trip). */
function _setSyncButtonsPending(activeMode) {
  _syncClickPending = activeMode;
  const snapBtn = document.getElementById('btn-sync-snapshot');
  const cdcBtn = document.getElementById('btn-auto-sync-cdc');
  if (snapBtn) {
    snapBtn.disabled = true;
    if (activeMode === 'snapshot') snapBtn.innerHTML = '&#9654; Syncing&hellip;';
  }
  if (cdcBtn) {
    cdcBtn.disabled = true;
    if (activeMode === 'cdc') cdcBtn.innerHTML = '&#9654; Syncing&hellip;';
  }
}

function _clearSyncClickPending() {
  _syncClickPending = null;
}

async function _refreshSyncButtonState() {
  await Promise.all([checkSyncStatus(), loadAutoCdcSection()]);
}

function _startSnapshotPoll() {
  const poller = setInterval(async () => {
    await checkSyncStatus();
    const el = document.getElementById('sync-status-display');
    if (el && (el.innerHTML.includes('COMPLETE') || el.innerHTML.includes('FAILED') || el.innerHTML.includes('PARTIAL'))) {
      clearInterval(poller);
      _clearSyncClickPending();
      await _refreshSyncButtonState();
      loadRecentRuns('sync-recent-runs', 'snapshot');
    }
  }, 10000);
}

function _startCdcPoll() {
  const poller = setInterval(async () => {
    await loadAutoCdcSection();
    const el = document.getElementById('auto-cdc-status-display');
    if (el && (el.innerHTML.includes('COMPLETE') || el.innerHTML.includes('FAILED') || el.innerHTML.includes('PARTIAL'))) {
      clearInterval(poller);
      _clearSyncClickPending();
      await _refreshSyncButtonState();
      loadRecentRuns('auto-cdc-recent-runs', 'cdc');
    }
  }, 10000);
}

async function startSnapshotSync() {
  const snapBtn = document.getElementById('btn-sync-snapshot');
  if (!snapBtn || snapBtn.disabled) return;
  _setSyncButtonsPending('snapshot');
  if (await _postSync('/sync/snapshot', 'sync-msg', 'Sync Snapshot')) {
    await checkSyncStatus();
    _startSnapshotPoll();
  } else {
    _clearSyncClickPending();
    await _refreshSyncButtonState();
  }
}

async function startCdcSync() {
  const cdcBtn = document.getElementById('btn-auto-sync-cdc');
  if (!cdcBtn || cdcBtn.disabled) return;
  _setSyncButtonsPending('cdc');
  if (await _postSync('/sync/cdc', 'auto-cdc-msg', 'Manual Sync CDC')) {
    await _refreshSyncButtonState();
    _startCdcPoll();
  } else {
    _clearSyncClickPending();
    await _refreshSyncButtonState();
  }
}

/** @deprecated use startSnapshotSync */
async function startSync() {
  return startSnapshotSync();
}

function _updateSnapshotBtnState(data) {
  const SBS = window.SyncButtonState;
  _syncClickPending = SBS.nextSyncClickPending(_syncClickPending, data);
  const { disabled, label } = SBS.computeSnapshotBtnState(
    data, _syncClickPending, Date.now() / 1000,
  );

  for (const id of ['btn-sync-snapshot']) {
    const btn = document.getElementById(id);
    if (btn) { btn.disabled = disabled; btn.innerHTML = label; }
  }
}

function _updateAutoCdcBtnState(data) {
  const SBS = window.SyncButtonState;
  _syncClickPending = SBS.nextSyncClickPending(_syncClickPending, data);
  const { disabled, hintText, useGreen, label } = SBS.computeCdcBtnState(
    data, _syncClickPending,
  );

  const btn = document.getElementById('btn-auto-sync-cdc');
  const hint = document.getElementById('auto-cdc-disabled-hint');
  if (!btn) return;

  btn.disabled = disabled;
  btn.className = useGreen ? 'btn btn-success' : 'btn btn-outline';
  btn.innerHTML = label;

  if (hint) {
    hint.textContent = hintText;
    hint.style.display = (disabled && hintText) ? 'block' : 'none';
  }
}

function _formatScheduleTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : 'N/A';
}

function _autoCdcScheduleLine(data, active) {
  const lastAt = (active && active.last_run_at) || data.auto_cdc_last_run_at;
  const nextAt = (active && active.next_run_at) || data.auto_cdc_next_run_at;
  if (!lastAt && !nextAt) return null;
  return `Last run: ${_formatScheduleTime(lastAt)} \u00b7 Next run: ${_formatScheduleTime(nextAt)}`;
}

function _renderAutoCdcToggle(data, autoData) {
  const toggle = document.getElementById('auto-cdc-toggle');
  const label = document.getElementById('auto-cdc-toggle-label');
  const projLabel = document.getElementById('auto-cdc-project-label');
  const scheduleDetail = document.getElementById('auto-cdc-schedule-detail');
  if (!toggle || !label || !projLabel || !scheduleDetail) return;

  const hub = data.hub_name || '';
  const proj = data.project_name || '';
  projLabel.textContent = (hub || proj) ? `${hub} \u203a ${proj}` : '\u2014';

  const toggleOn = !!data.daily_cdc_enabled;
  const hasBaseline = !!data.has_snapshot_baseline;
  const active = (autoData.active_syncs || [])[0];

  toggle.classList.toggle('on', toggleOn);
  label.textContent = toggleOn ? 'ON' : 'OFF';

  if (!hasBaseline) {
    toggle.style.opacity = '0.45';
    toggle.style.cursor = 'not-allowed';
    scheduleDetail.textContent = 'Complete Sync Snapshot to enable Auto CDC.';
  } else {
    toggle.style.opacity = '1';
    toggle.style.cursor = 'pointer';
    if (toggleOn) {
      const scheduleLine = _autoCdcScheduleLine(data, active);
      scheduleDetail.textContent = scheduleLine
        || 'Auto CDC is on \u2014 daily incremental syncs scheduled.';
    } else {
      scheduleDetail.textContent = 'Auto CDC is off.';
    }
  }
}

async function loadAutoCdcSection() {
  const statusEl = document.getElementById('auto-cdc-status-display');
  if (!statusEl) return;

  let cdcRun = null;
  try {
    const [statusResp, autoResp] = await Promise.all([
      fetch('/sync/status'),
      fetch('/sync/auto'),
    ]);
    const data = await statusResp.json();
    const autoData = autoResp.ok ? await autoResp.json() : { active_syncs: [] };

    _renderAutoCdcToggle(data, autoData);
    _updateAutoCdcBtnState(data);

    cdcRun = data.cdc_run;
    statusEl.innerHTML = cdcRun
      ? _renderSyncRunDisplay(
          cdcRun,
          data.hub_name,
          data.project_name,
          'CDC service groups',
          _cdcTriggerLabel(cdcRun.trigger_type),
        )
      : '';
  } catch (e) {
    statusEl.innerHTML = `<p style="font-size:13px;color:#c0392b">Error loading Auto CDC status: ${_escapeHtml(e.message)}</p>`;
    return;
  }

  if (!cdcRun || _isRunTerminal(cdcRun.state)) {
    await loadRecentRuns('auto-cdc-recent-runs', 'cdc');
  }
}

async function toggleAutoCdc() {
  const statusResp = await fetch('/sync/status');
  const data = await statusResp.json();
  if (!data.has_snapshot_baseline) {
    msg('auto-cdc-msg', 'Complete Sync Snapshot before enabling Auto CDC.', 'error');
    return;
  }

  const toggle = document.getElementById('auto-cdc-toggle');
  const isOn = toggle && toggle.classList.contains('on');
  await toggleAutoSync(!isOn);
}

async function checkSyncStatus() {
  const resp = await fetch('/sync/status');
  const data = await resp.json();
  const el = document.getElementById('sync-status-display');
  if (!el) return;

  _updateSnapshotBtnState(data);
  _updateAutoCdcBtnState(data);

  if (!resp.ok || !data.snapshot_run) {
    el.innerHTML = '';
    return;
  }

  const r = data.snapshot_run;
  el.innerHTML = _renderSyncRunDisplay(
    r,
    data.hub_name,
    data.project_name,
    'all service groups',
    null,
  );

  if (r.state === 'complete' || r.state === 'failed') {
    fetch('/state').then(rr => rr.json()).then(sd => {
      if (sd.has_sync_history && !_hasSyncHistory) {
        _hasSyncHistory = true;
        setNavDone(3);
        unlockNav(4);
        unlockNav(5);
      }
      loadRecentRuns('sync-recent-runs', 'snapshot');
    }).catch(() => {});
  }
}

// ─────────────────────────────────────────────
// Recent runs + Data Connector request list
// ─────────────────────────────────────────────
function _syncRunMode(run) {
  if (!run) return 'snapshot';
  const dt = run.data_types || '';
  if (dt.includes('data_connector_cdc')) return 'cdc';
  try {
    const rc = JSON.parse(run.record_counts || '{}');
    if (rc.mode === 'cdc') return 'cdc';
    if (rc.mode === 'snapshot') return 'snapshot';
  } catch (_) {}
  return 'snapshot';
}

function _runTypeLabel(run) {
  const mode = _syncRunMode(run);
  if (mode === 'cdc') {
    return String(run.trigger_type || '').toLowerCase() === 'auto' ? 'Daily CDC' : 'Manual CDC';
  }
  return 'Sync Snapshot';
}

function _renderRecentRunsTable(runs, containerId, limit = 8) {
  const histEl = document.getElementById(containerId);
  if (!histEl) return;

  const slice = (runs || []).slice(0, limit);
  if (!slice.length) {
    const emptyMsg = containerId === 'auto-cdc-recent-runs'
      ? 'No CDC runs recorded yet.'
      : 'No snapshot runs recorded yet.';
    histEl.innerHTML = `<p style="font-size:13px;color:#aaa;">${emptyMsg}</p>`;
    return;
  }

  const rows = slice.map(r => {
    let rc = {};
    try { rc = JSON.parse(r.record_counts || '{}'); } catch (_) {}
    const nFiles = rc.files !== undefined && rc.files !== null ? Number(rc.files) : '—';
    const status = r.state === 'complete' ? '<span class="badge badge-success">Success</span>'
                 : r.state === 'failed' ? '<span class="badge badge-failed">Failed</span>'
                 : `<span class="badge badge-running">${_escapeHtml(r.state)}</span>`;
    const started = new Date(r.started_at * 1000).toLocaleString();
    const dur = r.state === 'complete' || r.state === 'failed'
      ? `${Math.round((r.updated_at - r.started_at))}s` : '—';
    const typ = _runTypeLabel(r);
    return `<tr>
              <td>#${_runDisplayNumber(r)}</td>
              <td>${_escapeHtml(typ)}</td>
              <td>${_escapeHtml(started)}</td>
              <td>${status}</td>
              <td>${typeof nFiles === 'number' ? nFiles : _escapeHtml(nFiles)}</td>
              <td>${_escapeHtml(dur)}</td>
            </tr>`;
  }).join('');

  histEl.innerHTML = `<table class="history-table">
    <thead><tr><th>Run</th><th>Type</th><th>Started</th><th>Status</th><th>CSV files</th><th>Duration</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function loadRecentRuns(containerId, modeFilter, limit = 8) {
  try {
    const resp = await fetch('/dashboard/data');
    if (!resp.ok) return;
    const d = await resp.json();
    let runs = (d.recent_runs && d.recent_runs.length) ? d.recent_runs : (d.manual_history || []);
    runs = runs.filter(r => _isRunTerminal(r.state));
    if (modeFilter) {
      runs = runs.filter(r => _syncRunMode(r) === modeFilter);
    }
    _renderRecentRunsTable(runs, containerId, limit);
  } catch (_) {
    const histEl = document.getElementById(containerId);
    if (histEl) {
      histEl.innerHTML = '<p style="font-size:13px;color:#c0392b;">Failed to load recent runs.</p>';
    }
  }
}

async function loadDCRequests() {
  const el = document.getElementById('dc-requests-list');
  if (!el) return;
  el.innerHTML = '<p style="font-size:13px;color:#aaa;padding:4px 0">Loading&hellip;</p>';
  try {
    const resp = await fetch('/dc/requests');
    const data = await resp.json();
    if (!resp.ok) {
      el.innerHTML = `<p style="font-size:13px;color:#c0392b">${data.error || 'Failed to load requests'}</p>`;
      return;
    }
    _renderDCRequests(data.requests || []);
  } catch (e) {
    el.innerHTML = `<p style="font-size:13px;color:#c0392b">Error: ${e.message}</p>`;
  }
}

function _renderDCRequests(reqs) {
  const el = document.getElementById('dc-requests-list');
  if (!reqs.length) {
    el.innerHTML = '<p style="font-size:13px;color:#aaa;padding:4px 0">No active ACC export jobs for this project.</p>';
    return;
  }
  // Sort newest first using createdAt or effectiveFrom
  reqs.sort((a, b) => {
    const ta = a.createdAt || a.effectiveFrom || '';
    const tb = b.createdAt || b.effectiveFrom || '';
    return tb.localeCompare(ta);
  });
  el.innerHTML = reqs.map(r => {
    const created = r.createdAt   ? new Date(r.createdAt).toLocaleString()
                  : r.effectiveFrom ? new Date(r.effectiveFrom).toLocaleString()
                  : '—';
    const rawStatus = r.status || 'unknown';
    const statusLo  = rawStatus.toLowerCase();
    const cls = statusLo === 'complete'                               ? 'state-complete'
              : ['failed','error','cancelled','canceled'].includes(statusLo) ? 'state-failed'
              : ['pending','queued','inprogress','processing',
                 'waiting','active','running'].includes(statusLo)    ? 'state-running'
              : 'state-pending';
    const canDelete = !['complete','failed','error','cancelled','canceled'].includes(statusLo);
    const delBtn = canDelete
      ? `<button onclick="deleteDCRequest('${r.id}')"
           style="padding:4px 12px;border:1px solid #dc3545;background:transparent;
                  color:#dc3545;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;
                  flex-shrink:0">Delete</button>`
      : '';
    const shortId = r.id && r.id.length > 12 ? `${r.id.slice(0, 8)}&hellip;` : (r.id || '—');
    return `<div style="background:#f8f9fa;border-radius:8px;padding:11px 14px;
                        margin-bottom:8px;display:flex;align-items:center;gap:10px">
      <div style="flex:1;min-width:0">
        <div style="font-size:11px;font-family:monospace;color:#555;
                    overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
             title="${_escapeHtml(r.id)}">${shortId}</div>
        <div style="font-size:11px;color:#aaa;margin-top:2px">${created}</div>
      </div>
      <span class="state-tag ${cls}" style="font-size:11px;flex-shrink:0">${rawStatus}</span>
      ${delBtn}
    </div>`;
  }).join('');
}

async function deleteDCRequest(requestId) {
  if (!confirm('Delete this Data Connector request? This will cancel the export job.')) return;
  try {
    const resp = await fetch(`/dc/requests/${encodeURIComponent(requestId)}`, { method: 'DELETE' });
    const data = await resp.json();
    if (!resp.ok) {
      alert(`Failed to delete: ${data.error || resp.status}`);
      return;
    }
    loadDCRequests();
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

// ─────────────────────────────────────────────
// On page load — restore state from server
// ─────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  _initStartOverModal();

  const resp = await fetch('/state');
  const data = await resp.json();

  _accConnected    = data.acc_token_present;
  _accConfigSaved  = data.acc_connected;
  _dbxBootstrapped = data.dbx_bootstrapped;
  _hasSyncHistory  = data.has_sync_history || false;
  _dbxTokenPresent = data.dbx_token_present;
  _dbxWorkspaceUrl = data.dbx_workspace_url || '';

  const ucs = document.getElementById('uc-catalog-select');
  if (ucs) {
    ucs.addEventListener('change', () => {
      const b = document.getElementById('btn-start-provisioning');
      if (b) b.disabled = !ucs.value;
    });
  }

  // Pre-fill workspace URL input and cloud badge if tokens already saved
  if (data.dbx_workspace_url) {
    const inp = document.getElementById('workspace-url-input');
    if (inp) { inp.value = data.dbx_workspace_url; onWorkspaceUrlInput(); }
  }

  // Credentials status — show saved state / prefill the workspace URL in
  // the Connect Databricks panel.
  try {
    await loadDbxCredsStatus();
  } catch (e) { /* non-critical */ }

  // Unlock sidebar items based on state
  if (_accConfigSaved) {
    setNavDone(1);
    unlockNav(2);
    if (data.project_name) {
      document.getElementById('acc-done-pill').innerHTML = `&#10003; Connected &mdash; ${data.project_name}`;
      document.getElementById('acc-done-pill').className = 'status-pill pill-connected';
      document.getElementById('acc-done-detail').innerHTML =
        `<strong>Hub:</strong> ${data.hub_name || ''}<br>
         <strong>Project:</strong> ${data.project_name || ''}`;
    }
  }
  if (_dbxBootstrapped) {
    setNavDone(2);
    unlockNav(3);
    document.getElementById('dbx-status-pill').className = 'status-pill pill-connected';
    document.getElementById('dbx-status-pill').innerHTML = '&#10003; Databricks connected &mdash; Bootstrap complete';
    for (let i = 1; i <= 10; i++) {
      const d = document.getElementById(`bs-dot-${i}`);
      if (d) d.className = 'bs-dot done';
    }
    document.getElementById('bootstrap-progress').style.display = 'block';
  }
  if (_hasSyncHistory) {
    setNavDone(3);
    unlockNav(4);
    unlockNav(5);
  }

  // Returning from Databricks OAuth — choose catalog, then provisioning
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('dbx_connected') === '1' && _dbxTokenPresent && !_dbxBootstrapped) {
    navTo(2);
    loadUcCatalogs();
    if (window.history.replaceState) window.history.replaceState({}, '', '/');
    return;
  }

  // Determine which panel to show on load
  if (_hasSyncHistory) {
    navTo(5);
  } else if (_dbxBootstrapped) {
    navTo(3);
  } else if (_accConfigSaved) {
    navTo(2);
    if (_dbxTokenPresent && !_dbxBootstrapped) {
      loadUcCatalogs();
    }
  } else if (_accConnected) {
    // Token present (fresh OAuth or returning session) — show hub/project selection
    navTo(1);
    await loadHubs();
  } else {
    // Not connected — show Sign In CTA
    showPanel('1a');
  }
});

// ─────────────────────────────────────────────
// Step 5 — Dashboard
// ─────────────────────────────────────────────
function _dashEscape(s) {
  if (s === null || s === undefined) return '';
  const t = String(s);
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toggleDashAdvanced() {
  _dashAdvancedOpen = !_dashAdvancedOpen;
  const body = document.getElementById('dash-advanced-body');
  const toggle = document.getElementById('dash-advanced-toggle');
  if (body) body.style.display = _dashAdvancedOpen ? 'block' : 'none';
  if (toggle) {
    toggle.innerHTML = _dashAdvancedOpen
      ? '&#9660; Advanced: Data Connector Requests'
      : '&#9658; Advanced: Data Connector Requests';
  }
  if (_dashAdvancedOpen) loadDCRequests();
}

async function _renderDashAutoCdcSummary() {
  const el = document.getElementById('dash-auto-cdc-summary');
  if (!el) return;
  try {
    const [statusResp, autoResp] = await Promise.all([
      fetch('/sync/status'),
      fetch('/sync/auto'),
    ]);
    const status = await statusResp.json();
    const autoData = autoResp.ok ? await autoResp.json() : { active_syncs: [] };
    const active = (autoData.active_syncs || [])[0];
    const on = !!status.daily_cdc_enabled;

    if (on) {
      const scheduleLine = _autoCdcScheduleLine(status, active);
      el.innerHTML = scheduleLine
        ? `<strong>Auto CDC:</strong> ON &nbsp;&middot;&nbsp; ${scheduleLine} &nbsp;&mdash;&nbsp; <a onclick="navTo(4)">Manage in Step 4</a>`
        : `<strong>Auto CDC:</strong> ON &nbsp;&mdash;&nbsp; <a onclick="navTo(4)">Manage in Step 4</a>`;
    } else {
      el.innerHTML = `<strong>Auto CDC:</strong> OFF &nbsp;&mdash;&nbsp; <a onclick="navTo(4)">Enable in Step 4</a>`;
    }
  } catch (_) {
    el.innerHTML = '';
  }
}

async function loadDashboard() {
  const resp = await fetch('/dashboard/data');
  if (!resp.ok) return;
  const d = await resp.json();

  const sub = document.getElementById('dash-subtitle');
  if (sub && (d.hub_name || d.project_name)) {
    sub.textContent = `${d.hub_name || ''} › ${d.project_name || ''}`;
  }

  const st = d.stats || {};
  const stateLo = (st.latest_state || '').toLowerCase();
  const term = st.terminal_run_count !== undefined ? st.terminal_run_count : st.manual_terminal_runs;

  const intro = document.getElementById('dash-intro');
  if (intro) {
    intro.style.display = (_hasSyncHistory || stateLo === 'complete') ? 'none' : 'block';
  }

  await _renderDashAutoCdcSummary();

  const dbxEl = document.getElementById('dash-dbx-context');
  const ctxDbx = d.databricks_context || {};
  const lk = ctxDbx.links || {};
  if (ctxDbx.workspace_url) {
    const cat = ctxDbx.catalog_name || '—';
    const vol = ctxDbx.volume_path || '—';
    const cloud = ctxDbx.cloud_provider ? ` (${_dashEscape(ctxDbx.cloud_provider)})` : '';
    const cdcLink = lk.cdc_pipeline
      ? ` &nbsp;&middot;&nbsp; <a href="${_dashEscape(lk.cdc_pipeline)}" target="_blank" rel="noopener noreferrer">CDC pipeline</a>`
      : '';
    dbxEl.innerHTML = `<div style="background:#f4f7fb;border-radius:10px;padding:14px 16px;border:1px solid #e2e8f0;height:100%">
      <div style="font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px">Databricks landing zone</div>
      <div style="font-size:13px;line-height:1.7;color:#334155">
        <div><strong>Workspace</strong>${cloud}: <a href="${_dashEscape(lk.workspace || ctxDbx.workspace_url)}" target="_blank" rel="noopener noreferrer">Open workspace</a></div>
        <div><strong>Unity Catalog</strong>: <code style="font-size:12px">${_dashEscape(cat)}</code></div>
        <div><strong>Volume path</strong>: <code style="font-size:11px;word-break:break-all">${_dashEscape(vol)}</code></div>
        <div style="margin-top:8px">
          ${lk.snapshot_pipeline ? `<a href="${_dashEscape(lk.snapshot_pipeline)}" target="_blank" rel="noopener noreferrer">Snapshot Pipeline</a>${cdcLink}` : ''}
        </div>
      </div>
    </div>`;
  } else if (dbxEl) {
    dbxEl.innerHTML = '<p style="font-size:13px;color:#94a3b8;">Connect Databricks (Step 2) to show workspace and Unity Catalog paths.</p>';
  }

  const ch = d.chart || { labels: [], files: [] };
  const chartLabels = ch.labels || [];
  const chartData = ch.files || [];
  const canvas = document.getElementById('dash-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (_dashChart) _dashChart.destroy();
  if (_dashChartOutcomes) _dashChartOutcomes.destroy();

  if (!chartLabels.length) {
    _dashChart = new Chart(ctx, {
      type: 'bar',
      data: { labels: ['—'], datasets: [{ label: 'CSV files', data: [0], backgroundColor: '#cbd5e1' }] },
      options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } }
    });
  } else {
    _dashChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: chartLabels,
        datasets: [{
          label: 'CSV files',
          data: chartData,
          borderColor: '#0066cc',
          backgroundColor: 'rgba(0,102,204,0.12)',
          fill: true,
          tension: 0.2,
          pointRadius: 4,
        }]
      },
      options: {
        responsive: true,
        plugins: { legend: { display: true } },
        scales: {
          x: { ticks: { maxRotation: 45, minRotation: 0, autoSkip: true } },
          y: { beginAtZero: true, ticks: { precision: 0 } },
        },
      }
    });
  }

  const co = d.chart_outcomes || { success: 0, failed: 0 };
  const succ = Number(co.success) || 0;
  const fail = Number(co.failed) || 0;
  const outcomesSection = document.getElementById('dash-outcomes-section');
  const wrapEl = document.getElementById('dash-chart-outcomes-wrap');
  const emptyEl = document.getElementById('dash-chart-outcomes-empty');
  const outCanvas = document.getElementById('dash-chart-outcomes');
  const showOutcomes = term >= 3 && (succ + fail) > 0;

  if (outcomesSection) outcomesSection.style.display = showOutcomes ? 'block' : 'none';

  if (showOutcomes && wrapEl && emptyEl && outCanvas) {
    wrapEl.style.display = 'block';
    emptyEl.style.display = 'none';
    const octx = outCanvas.getContext('2d');
    _dashChartOutcomes = new Chart(octx, {
      type: 'doughnut',
      data: {
        labels: ['Succeeded', 'Failed'],
        datasets: [{
          data: [succ, fail],
          backgroundColor: ['#22c55e', '#ef4444'],
          borderWidth: 2,
          borderColor: '#fff',
        }],
      },
      options: {
        responsive: true,
        plugins: {
          legend: { position: 'bottom' },
        },
      },
    });
  }

  if (_dashAdvancedOpen) loadDCRequests();
}

async function toggleAutoSync(enable, projectId, msgEl) {
  const targetMsg = msgEl || 'auto-cdc-msg';
  const body = { enabled: enable };
  if (projectId) body.project_id = projectId;
  const resp = await fetch('/sync/auto', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  const data = await resp.json();
  if (!resp.ok) {
    msg(targetMsg, data.error || 'Failed to toggle Auto CDC', 'error');
    return;
  }
  msg(targetMsg, data.message || 'Auto CDC updated', 'success');
  if (document.getElementById('auto-cdc-toggle')) {
    await loadAutoCdcSection();
  }
  if (_activeStep === 5) {
    loadDashboard();
  }
}
