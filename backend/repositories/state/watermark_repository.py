
import time
from .database import _conn

# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------

def get_watermark(user_id: str, project_id: str, data_type: str) -> float | None:
    with _conn() as con:
        row = con.execute('''
            SELECT last_sync FROM watermarks
            WHERE user_id = ? AND project_id = ? AND data_type = ?
        ''', (user_id, project_id, data_type)).fetchone()
    return row['last_sync'] if row else None


def set_watermark(user_id: str, project_id: str, data_type: str, ts: float = None) -> None:
    ts = ts or time.time()
    with _conn() as con:
        con.execute('''
            INSERT INTO watermarks (user_id, project_id, data_type, last_sync)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, project_id, data_type) DO UPDATE SET last_sync = excluded.last_sync
        ''', (user_id, project_id, data_type, ts))


