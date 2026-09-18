from flask import session, abort


def _current_user_id():
    """Return the authenticated user_id from the Flask session, or None."""
    return session.get('user_id')


def _require_user_id():
    """Return the session user_id or 401 the request. Use on JSON API routes."""
    uid = session.get('user_id')
    
    if not uid:
        abort(401, description='Not authenticated. Connect ACC first.')
    return uid