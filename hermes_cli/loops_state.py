"""Compare-and-swap tick writes: controls/replacements win over stale executor results."""


def save_current_tick(session_id, state, expected):
    from hermes_cli.loops import _get_session_db, _meta_key, _parse_state
    db = _get_session_db()
    if db is None or expected is None:
        return False
    key = _meta_key(session_id)
    raw = db.get_meta(key)
    current = _parse_state(raw, session_id) if raw else None
    if current is None or current.status != 'active' or current.to_json() != expected:
        return False
    return db._write_rowcount('UPDATE state_meta SET value=? WHERE key=? AND value=?',
                             (state.to_json(), key, raw)) == 1
