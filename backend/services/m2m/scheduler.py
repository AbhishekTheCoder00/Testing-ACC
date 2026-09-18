"""
Purpose: Turns "daily CDC" into something that happens without anyone logged in (FR-03 §11.4).
One tick picks every connection that is ready, CDC-enabled, has a service principal and is due,
then runs each one independently so a single broken connection cannot stop the fleet. The next
run time is claimed *before* the sync so overlapping ticks cannot double-run a connection and a
permanently failing one cannot become a hot retry loop.
"""

from __future__ import annotations

import logging
import time

from backend.repositories.state import (
    connection_repository as conns,
    connection_sync_repository as runs,
)
from backend.services.m2m import connection_runner
from backend.services.m2m.connection_service import CDC_INTERVAL_SEC

logger = logging.getLogger(__name__)


def scheduled_cdc_tick(now: float | None = None) -> dict:
    """Run one scheduler pass. Returns what happened, for the route to echo and for ops.

    Eligibility lives in ``connection_repository.list_due_for_cdc``, which joins on the
    service-principal row — the D-4 guard that keeps a cron tick off any connection that
    would need a human's Databricks token.
    """
    tick_at = time.time() if now is None else now
    due = conns.list_due_for_cdc(tick_at)
    summary = {'started': [], 'skipped': [], 'failed': [], 'due': len(due)}

    for connection in due:
        connection_id = connection['connection_id']

        in_flight = runs.has_in_flight_run(connection_id)
        if in_flight:
            # Leave next_run_at alone so this connection is retried on the following tick.
            logger.info(
                'Scheduler: skipping %s, run %s is still %s',
                connection_id, in_flight['run_id'], in_flight['state'],
            )
            summary['skipped'].append(connection_id)
            continue

        conns.set_schedule(
            connection_id, enabled=True, next_run_at=tick_at + CDC_INTERVAL_SEC,
        )

        try:
            connection_runner.run_connection_sync(
                connection_id, mode='cdc', trigger_type='scheduled',
            )
            summary['started'].append(connection_id)
        except Exception as exc:
            # Deliberately swallowed: the run row already carries the error, and the next
            # connection in this tick has nothing to do with this failure.
            logger.exception('Scheduler: CDC sync failed for %s: %s', connection_id, exc)
            summary['failed'].append(connection_id)

    logger.info(
        'Scheduler tick: due=%d started=%d skipped=%d failed=%d',
        summary['due'], len(summary['started']), len(summary['skipped']),
        len(summary['failed']),
    )
    return summary
