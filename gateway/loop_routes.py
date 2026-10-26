"""Resolve loop wakes from owning routing entries before busy/admission selection.

A transcript ID may have several route aliases. Missing historical workspace fields
can only be filled by one matching persisted owner, never by the connected bot roster.
"""
from gateway.config import Platform
from gateway.session_identity import identity_of, replace_source

_ROUTE_FIELDS = ('platform', 'chat_id', 'chat_type', 'thread_id', 'user_id', 'profile',
                 'scope_id', 'guild_id')


def capture_loop_route(runner, source, entry):
    route = {k: v for k, v in source.to_dict().items()
             if k in (*_ROUTE_FIELDS, 'user_name') and v}
    route['session_key'] = runner._session_key_for_source(source)
    transport = getattr(entry, 'transport_profile', None)
    if transport:
        route['transport_profile'] = transport
    return route


def _matches(route, source, entry):
    values = source.to_dict()
    # Omitted thread/user in historical captures means no thread/user, not a wildcard.
    for key in _ROUTE_FIELDS:
        expected = route.get(key)
        if key in ('thread_id', 'user_id') or expected:
            if (values.get(key) or '') != (expected or ''):
                return False
    return (not route.get('session_key') or route['session_key'] == entry.session_key) and (
        not route.get('transport_profile') or route['transport_profile'] == entry.transport_profile)


def resolve_loop_route(runner, sid, route, profile):
    """Return source/key/adapter with verified owner continuity, or None. Read-only;
    callers run this off-loop in the scanned profile's scope. Lookup errors propagate.
    """
    if not isinstance(route, dict) or not route.get('platform') or not route.get('chat_id'):
        return None
    entries = [entry for entry in runner.session_store.list_sessions()
               if entry.session_id == sid and not entry.suspended and entry.origin is not None]
    candidates = [entry for entry in entries if _matches(route, entry.origin, entry)]
    if len(candidates) != 1:
        return None
    entry = candidates[0]
    # Reuse the persisted-origin restoration seam, including transport/profile identity.
    source = runner._restored_source(entry)
    if source is None or not _matches(route, source, entry):
        return None
    if source.platform == Platform.SLACK and not source.scope_id:
        return None
    identity = identity_of(source)
    runtime = identity.runtime_profile if identity is not None else source.profile
    if (runtime or 'default') != (profile or getattr(runner, '_primary_profile_name', None) or 'default'):
        return None
    if runner.session_store._generate_session_key(source) != entry.session_key:
        return None
    adapter = runner._adapter_for_source(source)
    if adapter is None:
        return None
    return replace_source(source, message_id=None), entry.session_key, adapter


def is_loop_tick_event(event, state, session_key):
    """An unrelated/setup turn or an older loop's completion must not settle this tick."""
    import json
    from hermes_cli.loops import tick_identity
    try:
        identity = json.loads(event.message_id or '')
    except (TypeError, ValueError):
        return False
    return event.internal and identity == ['loop-tick', session_key, tick_identity(state)]


async def dispatch_loop_tick(runner, adapter, source, key, sid, state, wakeup):
    """Canonical owners refuse missing provenance rather than falling into a parallel turn."""
    import json
    from gateway.session_authorities import active_authority
    from gateway.session_automation import admit_route_wake
    from hermes_cli.loops import tick_identity
    from hermes_state_runtime import RuntimeStoreError
    if not wakeup.lstrip().startswith('/'):
        if await admit_route_wake(runner, adapter, source, wakeup, 'loop-tick',
                                  wake_id=tick_identity(state), expected_session_id=sid):
            return
        if active_authority(runner) is not None:
            raise RuntimeStoreError('not_found')
    if runner.session_store.peek_session_id(key) != sid:
        raise RuntimeStoreError('admission_conflict')
    event = runner._synthetic_prompt_event(source, wakeup, internal=True)
    event.message_id = json.dumps(['loop-tick', key, tick_identity(state)])
    await adapter.handle_message(event)
