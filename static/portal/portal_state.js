/*
 * Purpose: Pure presentation logic for the portal — badge classes, relative times, the wizard
 * step a hub should resume at, and the enable/disable rules from FR-06 §4.11. Extracted from
 * app.js with no DOM or fetch so it can be unit tested under Node, the same way
 * sync_button_state.js is for the U2M page.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.portalState = api;
})(typeof self !== 'undefined' ? self : this, function () {

  // FR-06 §2.3 — tenant badge colour map, exact.
  const TENANT_BADGE = {
    ssa_active:           ['bg-emerald-100 text-emerald-700', 'ssa active'],
    ssa_provision_failed: ['bg-rose-100 text-rose-700',       'ssa provision failed'],
    ssa_limit_reached:    ['bg-rose-100 text-rose-700',       'ssa limit reached'],
    pending_whitelist:    ['bg-amber-100 text-amber-700',     'pending whitelist'],
    whitelist_verified:   ['bg-blue-100 text-blue-700',       'whitelist verified'],
  };

  const CONNECTION_BADGE = {
    ready:                       'bg-emerald-100 text-emerald-700',
    pending_bootstrap:           'bg-blue-100 text-blue-700',
    pending_databricks:          'bg-amber-100 text-amber-700',
    pending_ssa:                 'bg-amber-100 text-amber-700',
    pending_custom_integration:  'bg-amber-100 text-amber-700',
    running:                     'bg-blue-100 text-blue-700',
  };

  function tenantBadge(status) {
    if (!status) return { cls: 'bg-slate-100 text-slate-600', label: 'Not set up' };
    const hit = TENANT_BADGE[status];
    if (hit) return { cls: hit[0], label: hit[1] };
    return { cls: 'bg-slate-100 text-slate-600', label: String(status).replace(/_/g, ' ') };
  }

  function statusBadge(status) {
    return CONNECTION_BADGE[status] || 'bg-slate-100 text-slate-600';
  }

  // FR-06 §3.3 — where the wizard resumes for a hub.
  function setupStepFor(tenantStatus) {
    if (!tenantStatus || tenantStatus === 'pending_whitelist') return 1;
    // Only `ssa_active` means a robot exists. `whitelist_verified` still owes step 2, where the
    // admin is shown the robot email to invite into the hub (FR-06 §3.3).
    if (tenantStatus === 'ssa_active') return 3;
    return 2; // whitelist_verified | ssa_provision_failed | ssa_limit_reached
  }

  function needsSetup(hub) {
    return !hub || !hub.onboarding_status || hub.onboarding_status !== 'ssa_active';
  }

  /* The Databricks authorize leg is a full-page redirect, so nothing the wizard holds in memory
     survives it. The callback returns to /portal?dbx=1; this decides whether the stash written
     before leaving still applies. Without it setupStepFor() recomputes from the tenant status,
     which stays `whitelist_verified` until a connection is ready — dropping the admin back on
     step 2 (SSA) every time they authorize Databricks. */
  function resumeAfterDatabricks(search, stash, hubId) {
    if (new URLSearchParams(search || '').get('dbx') !== '1') return null;
    if (!stash || !stash.step || stash.hub_id !== hubId) return null;
    return { step: stash.step, form: stash.form || {} };
  }

  // FR-06 §6 — relative time buckets.
  function rel(epochSeconds, nowSeconds) {
    if (!epochSeconds) return 'never';
    const now = nowSeconds === undefined ? Date.now() / 1000 : nowSeconds;
    const d = Math.max(0, now - epochSeconds);
    if (d < 120) return 'just now';
    if (d < 7200) return `${Math.floor(d / 60)} minutes ago`;
    if (d < 172800) return `${Math.floor(d / 3600)} hours ago`;
    return `${Math.floor(d / 86400)} days ago`;
  }

  // The Auto CDC gate: a service principal AND a verified robot (ADR D-18). Returns the
  // specific blocker so the UI can name it instead of showing a generic error.
  function autoCdcBlocker(connection) {
    if (!connection) return 'no_connection';
    if (connection.onboarding_status !== 'ready') return 'not_ready';
    if (!connection.has_service_principal) return 'no_service_principal';
    if (!connection.robot_verified) return 'robot_unverified';
    return null;
  }

  const AUTO_CDC_MESSAGES = {
    no_connection:        'Create a connection first.',
    not_ready:            'Finish setting up this connection first.',
    no_service_principal: 'Add a Databricks service principal to enable scheduled sync.',
    robot_unverified:     'Invite the service account to this project in ACC to enable scheduled sync.',
  };

  function canSync(connection) {
    return !!connection && connection.onboarding_status === 'ready' && !connection.syncing;
  }

  // FR-06 §4.8 — which wizard step one *connection* resumes at. Mirrors
  // connection_service.next_step; the server's `next_step` is authoritative and this is the
  // fallback for a connection the client already holds.
  const CONNECTION_STEP = {
    pending_custom_integration: 1,
    pending_ssa: 2,
    pending_databricks: 3,
    pending_bootstrap: 4,
    ready: 5,
  };

  function connectionStepFor(status) {
    return CONNECTION_STEP[status] || 1;
  }

  // FR-05 §10.3 run states → the status pill. In-flight states are blue so a run in progress
  // never looks like a failure while it is still working.
  const RUN_PILL = {
    pending:          ['bg-blue-100 text-blue-700', 'PENDING'],
    exporting:        ['bg-blue-100 text-blue-700', 'DC_JOB_SUBMITTED'],
    downloading:      ['bg-blue-100 text-blue-700', 'UPLOADING_FILES'],
    pipeline_running: ['bg-blue-100 text-blue-700', 'PIPELINE_RUNNING'],
    success:          ['bg-emerald-100 text-emerald-700', 'COMPLETE'],
    failed:           ['bg-rose-100 text-rose-700', 'FAILED'],
    cancelled:        ['bg-slate-100 text-slate-600', 'CANCELLED'],
  };

  const IN_FLIGHT_RUN_STATES = ['pending', 'exporting', 'downloading', 'pipeline_running'];

  function runPill(state) {
    const hit = RUN_PILL[state];
    if (!hit) return { cls: 'bg-slate-100 text-slate-600', label: state ? String(state).toUpperCase() : 'NO RUNS' };
    return { cls: hit[0], label: hit[1] };
  }

  function isRunInFlight(run) {
    return !!run && IN_FLIGHT_RUN_STATES.indexOf(run.state) !== -1;
  }

  // The bootstrap checklist the wizard renders while provisioning runs. Indexes are the step
  // numbers bootstrap_steps reports through the progress callback.
  const BOOTSTRAP_STEPS = [
    'Validate Databricks workspace access',
    'SQL Warehouse RUNNING',
    'Validate Unity Catalog and metastore',
    'Verify catalog',
    'Create bronze schema',
    'Create volume',
    'Validate volume path',
    'Create _meta_ tables',
    'Upload pk_config_template.json',
    'Upload notebooks',
    'Create pipelines and workflows',
    'Save configuration',
  ];

  // FR-06 §4.11 — the validation matrix for the wizard's Continue buttons, as one function per
  // gate so each reason can be surfaced instead of a silently disabled button.
  function targetBlocker(form) {
    const f = form || {};
    if (!f.project_id) return 'no_project';
    if (!f.robot_verified) return 'robot_unverified';
    if (!f.workspace_url) return 'no_workspace';
    if (!/^https?:\/\/.+/.test(f.workspace_url)) return 'bad_workspace';
    if (f.dbx_client_id && !f.dbx_client_secret) return 'no_sp_secret';
    if (f.dbx_client_secret && !f.dbx_client_id) return 'no_sp_client_id';
    if (!f.catalog) return 'no_catalog';
    return null;
  }

  const TARGET_MESSAGES = {
    no_project:       'Choose an ACC project.',
    robot_unverified: 'Verify the service account on this project first.',
    no_workspace:     'Enter your Databricks workspace URL.',
    bad_workspace:    'The workspace URL must start with https://',
    no_sp_secret:     'Enter the service principal secret, or clear the client id.',
    no_sp_client_id:  'Enter the service principal client id, or clear the secret.',
    no_catalog:       'Pick a Unity Catalog.',
  };

  function nextRunLabel(connection, nowSeconds) {
    if (!connection || !connection.cdc_enabled) return 'Off';
    const at = connection.auto_cdc_next_run_at;
    if (!at) return 'Due now';
    const now = nowSeconds === undefined ? Date.now() / 1000 : nowSeconds;
    const d = at - now;
    if (d <= 0) return 'Due now';
    if (d < 3600) return `in ${Math.max(1, Math.round(d / 60))} minutes`;
    return `in ${Math.round(d / 3600)} hours`;
  }

  // FR-06 §4.7 — a signing key older than this is flagged for rotation.
  const KEY_AGE_WARN_DAYS = 89;

  function keyAgeWarning(robot, nowSeconds) {
    if (!robot) return null;
    const since = robot.rotated_at || robot.created_at;
    if (!since) return null;
    const now = nowSeconds === undefined ? Date.now() / 1000 : nowSeconds;
    const days = Math.floor((now - since) / 86400);
    return days > KEY_AGE_WARN_DAYS ? days : null;
  }

  return {
    tenantBadge, statusBadge, setupStepFor, needsSetup, resumeAfterDatabricks, rel,
    autoCdcBlocker, AUTO_CDC_MESSAGES, canSync,
    connectionStepFor, runPill, isRunInFlight, IN_FLIGHT_RUN_STATES, BOOTSTRAP_STEPS,
    targetBlocker, TARGET_MESSAGES, nextRunLabel, keyAgeWarning, KEY_AGE_WARN_DAYS,
  };
});
