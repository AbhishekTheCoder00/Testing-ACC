'use strict';

function isRunTerminal(state) {
  return ['complete', 'failed', 'partial'].includes(state);
}

function isRunActive(run) {
  return !!(run && run.state && !isRunTerminal(run.state));
}

function computeSnapshotBtnState(data, syncClickPending, nowSec) {
  const snapRun = data?.snapshot_run || data?.run;
  const cdcRun = data?.cdc_run;
  const snapActive = isRunActive(snapRun);
  const cdcActive = isRunActive(cdcRun);
  const anyActive = snapActive || cdcActive;
  const pending = !!syncClickPending;
  const nextAt = data?.manual_full_next_at;

  let disabled = anyActive || pending;
  let label = '&#9654; Sync Snapshot';
  if (anyActive || pending) {
    label = '&#9654; Syncing&hellip;';
  } else if (nextAt && nextAt > nowSec) {
    disabled = true;
    const hrs = Math.max(1, Math.ceil((nextAt - nowSec) / 3600));
    label = `&#9654; Available in ${hrs}h`;
  }

  return { disabled, label, clearPending: anyActive };
}

function computeCdcBtnState(data, syncClickPending) {
  const cdcRun = data?.cdc_run;
  const cdcActive = isRunActive(cdcRun);
  const snapRun = data?.snapshot_run || data?.run;
  const snapActive = isRunActive(snapRun);
  const toggleOn = !!(data?.daily_cdc_enabled);
  const hasBaseline = !!(data?.has_snapshot_baseline);
  const pending = !!syncClickPending;

  let disabled = true;
  let hintText = '';

  if (!hasBaseline) {
    hintText = 'Complete Sync Snapshot successfully before running Manual Sync CDC.';
  } else if (!toggleOn) {
    hintText = 'Enable Auto CDC to run Manual Sync CDC.';
  } else if (cdcActive || snapActive || pending) {
    hintText = 'A sync is already in progress.';
  } else {
    disabled = false;
  }

  const useGreen = hasBaseline && toggleOn;
  const label = (cdcActive || pending) ? '&#9654; Syncing&hellip;' : '&#9654; Manual Sync CDC';

  return { disabled, hintText, useGreen, label, clearPending: cdcActive || snapActive };
}

function nextSyncClickPending(syncClickPending, data) {
  if (!syncClickPending) return null;
  const snapRun = data?.snapshot_run || data?.run;
  const cdcRun = data?.cdc_run;
  if (isRunActive(snapRun) || isRunActive(cdcRun)) return null;
  if (isRunTerminal(snapRun?.state) || isRunTerminal(cdcRun?.state)) return null;
  return syncClickPending;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    isRunActive,
    computeSnapshotBtnState,
    computeCdcBtnState,
    nextSyncClickPending,
  };
}

if (typeof window !== 'undefined') {
  window.SyncButtonState = {
    isRunActive,
    computeSnapshotBtnState,
    computeCdcBtnState,
    nextSyncClickPending,
  };
}
