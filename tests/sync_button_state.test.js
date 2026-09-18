'use strict';

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');

const {
  computeSnapshotBtnState,
  computeCdcBtnState,
  nextSyncClickPending,
} = require('../static/sync_button_state.js');

describe('sync button state', () => {
  it('keeps snapshot button disabled while click is pending before server shows active run', () => {
    const data = { snapshot_run: null, cdc_run: null };
    const state = computeSnapshotBtnState(data, 'snapshot', 1_000);

    assert.equal(state.disabled, true);
    assert.match(state.label, /Syncing/);
    assert.equal(state.clearPending, false);
  });

  it('clears pending once server reports an active run', () => {
    const data = { snapshot_run: { state: 'running' }, cdc_run: null };
    const pending = nextSyncClickPending('snapshot', data);

    assert.equal(pending, null);
    const state = computeSnapshotBtnState(data, pending, 1_000);
    assert.equal(state.disabled, true);
    assert.equal(state.clearPending, true);
  });

  it('keeps manual CDC disabled while snapshot click is pending', () => {
    const data = {
      has_snapshot_baseline: true,
      daily_cdc_enabled: true,
      snapshot_run: null,
      cdc_run: null,
    };
    const state = computeCdcBtnState(data, 'snapshot');

    assert.equal(state.disabled, true);
    assert.equal(state.hintText, 'A sync is already in progress.');
  });

  it('clears pending once server reports a terminal run', () => {
    const data = { snapshot_run: { state: 'complete' }, cdc_run: null };
    const pending = nextSyncClickPending('snapshot', data);

    assert.equal(pending, null);
    const state = computeSnapshotBtnState(data, pending, 1_000);
    assert.equal(state.disabled, false);
    assert.match(state.label, /Sync Snapshot/);
  });
});
