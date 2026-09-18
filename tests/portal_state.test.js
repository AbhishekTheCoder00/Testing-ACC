/*
 * Purpose: Node test for static/portal/portal_state.js — the portal's pure display rules.
 * Run with `node tests/portal_state.test.js`, the same way sync_button_state.test.js runs.
 * Covers the FR-06 §2.3 badge maps, the §3.3 wizard resume step, the §6 relative-time buckets
 * and the Auto CDC gate from ADR D-18 (service principal AND verified robot).
 */
const test = require('node:test');
const assert = require('node:assert');
const s = require('../static/portal/portal_state.js');

test('tenant badge: exact FR-06 colour map', () => {
  assert.strictEqual(s.tenantBadge('ssa_active').cls, 'bg-emerald-100 text-emerald-700');
  assert.strictEqual(s.tenantBadge('pending_whitelist').cls, 'bg-amber-100 text-amber-700');
  assert.strictEqual(s.tenantBadge('whitelist_verified').cls, 'bg-blue-100 text-blue-700');
  assert.strictEqual(s.tenantBadge('ssa_limit_reached').cls, 'bg-rose-100 text-rose-700');
  assert.strictEqual(s.tenantBadge('ssa_provision_failed').cls, 'bg-rose-100 text-rose-700');
});

test('tenant badge: no tenant reads "Not set up"', () => {
  assert.strictEqual(s.tenantBadge(null).label, 'Not set up');
  assert.strictEqual(s.tenantBadge(undefined).label, 'Not set up');
});

test('tenant badge: unknown status degrades to underscores-as-spaces', () => {
  const b = s.tenantBadge('some_new_state');
  assert.strictEqual(b.cls, 'bg-slate-100 text-slate-600');
  assert.strictEqual(b.label, 'some new state');
});

test('connection badge map', () => {
  assert.strictEqual(s.statusBadge('ready'), 'bg-emerald-100 text-emerald-700');
  assert.strictEqual(s.statusBadge('pending_bootstrap'), 'bg-blue-100 text-blue-700');
  assert.strictEqual(s.statusBadge('pending_databricks'), 'bg-amber-100 text-amber-700');
  assert.strictEqual(s.statusBadge('nonsense'), 'bg-slate-100 text-slate-600');
});

test('wizard resumes at the right step', () => {
  assert.strictEqual(s.setupStepFor(null), 1);                    // brand-new hub
  assert.strictEqual(s.setupStepFor('pending_whitelist'), 1);
  assert.strictEqual(s.setupStepFor('ssa_provision_failed'), 2);  // retry provisioning
  assert.strictEqual(s.setupStepFor('ssa_limit_reached'), 2);
  assert.strictEqual(s.setupStepFor('whitelist_verified'), 2);   // robot email still to show
  assert.strictEqual(s.setupStepFor('ssa_active'), 3);
});

test('the Databricks redirect resumes step 3 instead of falling back to SSA', () => {
  const stash = { hub_id: 'b.hub-1', step: 3, form: { workspace_url: 'https://w.databricks.com' } };
  // The regression: hub status is still `whitelist_verified` after authorize, so without a
  // resume the wizard recomputes to step 2 and the admin lands back on the SSA page.
  assert.strictEqual(s.setupStepFor('whitelist_verified'), 2);
  const r = s.resumeAfterDatabricks('?dbx=1', stash, 'b.hub-1');
  assert.strictEqual(r.step, 3);
  assert.strictEqual(r.form.workspace_url, 'https://w.databricks.com');
});

test('resume only applies to the dbx round trip, and only to the stashed hub', () => {
  const stash = { hub_id: 'b.hub-1', step: 3, form: {} };
  assert.strictEqual(s.resumeAfterDatabricks('', stash, 'b.hub-1'), null);
  assert.strictEqual(s.resumeAfterDatabricks('?dbx=1', stash, 'b.hub-2'), null);
  assert.strictEqual(s.resumeAfterDatabricks('?dbx=1', null, 'b.hub-1'), null);
  assert.strictEqual(s.resumeAfterDatabricks('?dbx=1', { hub_id: 'b.hub-1' }, 'b.hub-1'), null);
});

test('needsSetup is only false once the hub is fully active', () => {
  assert.strictEqual(s.needsSetup(null), true);
  assert.strictEqual(s.needsSetup({ onboarding_status: 'pending_whitelist' }), true);
  assert.strictEqual(s.needsSetup({ onboarding_status: 'whitelist_verified' }), true);
  assert.strictEqual(s.needsSetup({ onboarding_status: 'ssa_active' }), false);
});

test('relative time buckets', () => {
  const now = 1_000_000;
  assert.strictEqual(s.rel(null, now), 'never');
  assert.strictEqual(s.rel(now - 30, now), 'just now');
  assert.strictEqual(s.rel(now - 119, now), 'just now');
  assert.strictEqual(s.rel(now - 600, now), '10 minutes ago');
  assert.strictEqual(s.rel(now - 7200, now), '2 hours ago');
  assert.strictEqual(s.rel(now - 172800, now), '2 days ago');
});

test('relative time never reads negative for a clock skew', () => {
  assert.strictEqual(s.rel(1_000_100, 1_000_000), 'just now');
});

test('Auto CDC gate names the specific blocker, not a generic error', () => {
  assert.strictEqual(s.autoCdcBlocker(null), 'no_connection');
  assert.strictEqual(
    s.autoCdcBlocker({ onboarding_status: 'pending_bootstrap' }), 'not_ready');
  assert.strictEqual(
    s.autoCdcBlocker({ onboarding_status: 'ready', has_service_principal: false }),
    'no_service_principal');
  assert.strictEqual(
    s.autoCdcBlocker({ onboarding_status: 'ready', has_service_principal: true,
                       robot_verified: false }),
    'robot_unverified');
  assert.strictEqual(
    s.autoCdcBlocker({ onboarding_status: 'ready', has_service_principal: true,
                       robot_verified: true }),
    null);
});

test('every Auto CDC blocker has a message', () => {
  for (const code of ['no_connection', 'not_ready', 'no_service_principal', 'robot_unverified']) {
    assert.ok(s.AUTO_CDC_MESSAGES[code], `missing message for ${code}`);
  }
});

test('sync is allowed only when ready and idle', () => {
  assert.strictEqual(s.canSync({ onboarding_status: 'ready' }), true);
  assert.strictEqual(s.canSync({ onboarding_status: 'ready', syncing: true }), false);
  assert.strictEqual(s.canSync({ onboarding_status: 'pending_bootstrap' }), false);
  assert.strictEqual(s.canSync(null), false);
});

test('a connection resumes at its own wizard step', () => {
  assert.strictEqual(s.connectionStepFor('pending_custom_integration'), 1);
  assert.strictEqual(s.connectionStepFor('pending_ssa'), 2);
  assert.strictEqual(s.connectionStepFor('pending_databricks'), 3);
  assert.strictEqual(s.connectionStepFor('pending_bootstrap'), 4);
  assert.strictEqual(s.connectionStepFor('ready'), 5);
  assert.strictEqual(s.connectionStepFor('nonsense'), 1);
});

test('an in-flight run never renders as a failure', () => {
  for (const state of s.IN_FLIGHT_RUN_STATES) {
    assert.strictEqual(s.runPill(state).cls, 'bg-blue-100 text-blue-700', state);
    assert.strictEqual(s.isRunInFlight({ state }), true, state);
  }
  assert.strictEqual(s.runPill('success').cls, 'bg-emerald-100 text-emerald-700');
  assert.strictEqual(s.runPill('failed').cls, 'bg-rose-100 text-rose-700');
  assert.strictEqual(s.isRunInFlight({ state: 'success' }), false);
  assert.strictEqual(s.isRunInFlight(null), false);
});

test('no run history reads "NO RUNS" rather than blank', () => {
  assert.strictEqual(s.runPill(null).label, 'NO RUNS');
  assert.strictEqual(s.runPill(undefined).label, 'NO RUNS');
});

test('the bootstrap checklist covers every reported step', () => {
  assert.strictEqual(s.BOOTSTRAP_STEPS.length, 12);
  s.BOOTSTRAP_STEPS.forEach((label) => assert.ok(label.length > 0));
});

test('the target gate names one blocker at a time, in form order', () => {
  const full = {
    project_id: 'p1', robot_verified: true,
    workspace_url: 'https://dbc-abc.cloud.databricks.com', catalog: 'main',
  };
  assert.strictEqual(s.targetBlocker(null), 'no_project');
  assert.strictEqual(s.targetBlocker({ ...full, project_id: '' }), 'no_project');
  assert.strictEqual(s.targetBlocker({ ...full, robot_verified: false }), 'robot_unverified');
  assert.strictEqual(s.targetBlocker({ ...full, workspace_url: '' }), 'no_workspace');
  assert.strictEqual(s.targetBlocker({ ...full, workspace_url: 'dbc-abc' }), 'bad_workspace');
  assert.strictEqual(s.targetBlocker({ ...full, catalog: '' }), 'no_catalog');
  assert.strictEqual(s.targetBlocker(full), null);
});

test('a service principal must be both halves or neither', () => {
  const full = {
    project_id: 'p1', robot_verified: true,
    workspace_url: 'https://dbc-abc.cloud.databricks.com', catalog: 'main',
  };
  assert.strictEqual(s.targetBlocker({ ...full, dbx_client_id: 'sp' }), 'no_sp_secret');
  assert.strictEqual(s.targetBlocker({ ...full, dbx_client_secret: 'x' }), 'no_sp_client_id');
  assert.strictEqual(
    s.targetBlocker({ ...full, dbx_client_id: 'sp', dbx_client_secret: 'x' }), null);
});

test('every target blocker has a message', () => {
  for (const code of Object.keys(s.TARGET_MESSAGES)) {
    assert.ok(s.TARGET_MESSAGES[code], `missing message for ${code}`);
  }
  const codes = ['no_project', 'robot_unverified', 'no_workspace', 'bad_workspace',
                 'no_sp_secret', 'no_sp_client_id', 'no_catalog'];
  for (const code of codes) assert.ok(s.TARGET_MESSAGES[code], code);
});

test('next run label', () => {
  const now = 1_000_000;
  assert.strictEqual(s.nextRunLabel(null, now), 'Off');
  assert.strictEqual(s.nextRunLabel({ cdc_enabled: false }, now), 'Off');
  assert.strictEqual(s.nextRunLabel({ cdc_enabled: true }, now), 'Due now');
  assert.strictEqual(
    s.nextRunLabel({ cdc_enabled: true, auto_cdc_next_run_at: now - 5 }, now), 'Due now');
  assert.strictEqual(
    s.nextRunLabel({ cdc_enabled: true, auto_cdc_next_run_at: now + 1800 }, now),
    'in 30 minutes');
  assert.strictEqual(
    s.nextRunLabel({ cdc_enabled: true, auto_cdc_next_run_at: now + 86400 }, now),
    'in 24 hours');
});

test('key age warns only past the threshold, and uses the rotation date', () => {
  const now = 1_000_000_000;
  const day = 86400;
  assert.strictEqual(s.keyAgeWarning(null, now), null);
  assert.strictEqual(s.keyAgeWarning({ created_at: now - 10 * day }, now), null);
  assert.strictEqual(s.keyAgeWarning({ created_at: now - 90 * day }, now), 90);
  // A rotated key is young again even though it was created long ago.
  assert.strictEqual(
    s.keyAgeWarning({ created_at: now - 400 * day, rotated_at: now - 2 * day }, now), null);
});
