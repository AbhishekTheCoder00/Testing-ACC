
import time
from .database import _conn
# ---------------------------------------------------------------------------
# ACC config (hub / project / folder selection)
# ---------------------------------------------------------------------------

def save_acc_config(user_id: str, hub_id: str, hub_name: str,
                    project_id: str, project_name: str,
                    folder_id: str = None, folder_name: str = None,
                    acc_account_id: str = None) -> None:
    with _conn() as con:
        # Check if the project is changing — if so, wipe watermarks to force a full re-sync
        existing = con.execute(
            'SELECT project_id FROM acc_config WHERE user_id = ?', (user_id,)
        ).fetchone()
        if existing and existing['project_id'] != project_id:
            con.execute(
                'DELETE FROM watermarks WHERE user_id = ?', (user_id,)
            )

        con.execute('''
            INSERT INTO acc_config
                (user_id, hub_id, hub_name, project_id, project_name,
                 folder_id, folder_name, acc_account_id, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                hub_id         = excluded.hub_id,
                hub_name       = excluded.hub_name,
                project_id     = excluded.project_id,
                project_name   = excluded.project_name,
                folder_id      = excluded.folder_id,
                folder_name    = excluded.folder_name,
                acc_account_id = excluded.acc_account_id,
                saved_at       = excluded.saved_at
        ''', (user_id, hub_id, hub_name, project_id, project_name,
              folder_id, folder_name, acc_account_id, time.time()))


def get_acc_config(user_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute('SELECT * FROM acc_config WHERE user_id = ?', (user_id,)).fetchone()
    return dict(row) if row else None


def set_daily_cdc_enabled(user_id: str, enabled: bool) -> None:
    """Persist the dashboard toggle for scheduled CDC-beta sync."""
    with _conn() as con:
        if enabled:
            con.execute(
                'UPDATE acc_config SET daily_cdc_enabled = ? WHERE user_id = ?',
                (1, user_id),
            )
        else:
            con.execute(
                '''UPDATE acc_config
                   SET daily_cdc_enabled = ?, auto_cdc_next_run_at = NULL
                   WHERE user_id = ?''',
                (0, user_id),
            )


def set_auto_cdc_next_run_at(user_id: str, next_run_at: float | None) -> None:
    """Persist the next scheduled Auto CDC run for UI display after restart."""
    with _conn() as con:
        con.execute(
            'UPDATE acc_config SET auto_cdc_next_run_at = ? WHERE user_id = ?',
            (next_run_at, user_id),
        )

def is_daily_cdc_enabled(user_id: str) -> bool:
    cfg = get_acc_config(user_id)
    if not cfg:
        return False
    return bool(cfg.get('daily_cdc_enabled'))


def list_daily_cdc_configs() -> list[dict]:
    """ACC configs with Auto CDC enabled — used to restore timers after restart."""
    with _conn() as con:
        rows = con.execute('''
            SELECT user_id, project_id, hub_name, project_name
            FROM acc_config
            WHERE daily_cdc_enabled = 1
        ''').fetchall()
    return [dict(r) for r in rows]

