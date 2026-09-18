
from .database import _conn

_USER_TABLES = (
    'sync_runs',
    'watermarks',
    'bootstrap_state',
    'dbx_tokens',
    'dbx_credentials',
    'acc_config',
    'acc_tokens',
)


def reset_user(user_id: str) -> None:
    """Delete all persisted state for one user. Other users in connector.db are untouched."""
    with _conn() as con:
        for table in _USER_TABLES:
            con.execute(f'DELETE FROM {table} WHERE user_id = ?', (user_id,))
        # Catalog claims are keyed by owner, not user_id. Dropping them frees
        # the catalog for someone else — a reset user no longer owns anything.
        con.execute(
            'DELETE FROM catalog_claims WHERE owner_user_id = ?', (user_id,),
        )
