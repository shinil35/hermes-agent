"""Offline Slack loop capture -> scan -> canonical FIFO, with a real scratch authority.

No connector, model or messaging calls. The authority scheduler is disabled at the last
boundary; admission restoration and post-turn bookkeeping are exercised explicitly.
"""
import time
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session_identity import replace_source
from hermes_cli import goals, loops
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_automation_notification_category import _authority


@pytest_asyncio.fixture
async def lane(tmp_path, monkeypatch):
    from gateway.session_ingress_context import native_callback, register_transport_home
    from plugins.platforms.slack.adapter import SlackAdapter

    runner, authority = await _authority(tmp_path, monkeypatch)
    adapter = SlackAdapter(PlatformConfig(enabled=True, token='fixture-only', typing_indicator=False))
    runner.adapters = {Platform.SLACK: adapter}
    runner._wire_adapter_handlers(adapter)
    register_transport_home(runner, None, tmp_path)
    monkeypatch.setenv('SLACK_ALLOWED_USERS', '42')
    source = adapter.build_source(chat_id='channel', chat_type='group', thread_id='thread',
                                  user_id='42', scope_id='workspace', message_id='human')
    event = MessageEvent(text='human', source=source, message_id='human')
    with native_callback(runner, event, tmp_path):
        await authority.admit_native(event)
    key = runner._session_key_for_source(source)
    sid = runner.session_store.peek_session_id(key)
    await runner._warm_goals_session_db('loop fixture')
    adapter.handle_message = AsyncMock(side_effect=AssertionError('unexpected classic dispatch'))
    yield runner, authority, adapter, source, key, sid
    authority.db.close()
    db = goals._DB_CACHE.pop(str(tmp_path), None)
    if db is not None and db is not authority.db:
        db.close()


async def _create(lane, *, native=False, legacy=False):
    runner, _authority, _adapter, source, _key, sid = lane
    if native:
        result = await runner._handle_loop_command(MessageEvent(text='/loop 5m poll fixture', source=source))
        assert 'Loop set' in result
    else:
        # Agent-created persisted route: scope alone must be consumed by the driver too.
        route = {k: v for k, v in source.to_dict().items() if v and k != 'message_id'}
        if legacy:
            route.pop('scope_id', None)
            route.pop('guild_id', None)
        loops.LoopManager(sid).set('poll fixture', interval_seconds=300, route=route)
    return loops.load_loop(sid)


def _automation(lane):
    _runner, authority, _adapter, _source, _key, sid = lane
    return [r for r in list_session_admissions(authority.db, session_id=authority.logical_owner(sid),
                                              pending_only=False)
            if (r['payload'].get('native_text_v1') or {}).get('automation')]


@pytest.mark.asyncio
async def test_native_capture_keeps_workspace_and_canonical_owner(lane):
    state = await _create(lane, native=True)
    assert state.route['scope_id'] == 'workspace'
    assert state.route['session_key'] == lane[4]


@pytest.mark.asyncio
@pytest.mark.parametrize('native,legacy', [(True, False), (False, False), (False, True)])
async def test_first_busy_scan_defers_then_idle_scan_admits_original_route(lane, native, legacy):
    runner, _authority, adapter, source, key, sid = lane
    state = await _create(lane, native=native, legacy=legacy)
    runner._running_agents[key] = object()
    held = await runner._turn_leases.acquire(sid, owner_key=key, generation=1)
    try:
        await runner._loop_wakeup_fire_one(sid, state, time.time(), set())
        assert not _automation(lane)
        assert loops.load_loop(sid).ticks_fired == 0
        assert not loops.load_loop(sid).awaiting_response
    finally:
        runner._running_agents.pop(key)
        runner._turn_leases.release(held)
    await runner._loop_wakeup_fire_one(sid, loops.load_loop(sid), time.time(), set())
    rows = _automation(lane)
    assert len(rows) == 1
    envelope = rows[0]['payload']['native_text_v1']
    assert envelope['route'] == key
    assert envelope['source']['scope_id'] == source.scope_id
    assert 'message_id' not in envelope['source']
    from gateway.session_envelope import restore_native
    from gateway.session_automation import check_automation_route
    restored = restore_native(rows[0]['payload'], runner)
    verified, route = check_automation_route(runner, rows[0]['payload'], sid, restored.source, adapter)
    assert route == key and verified.scope_id == 'workspace'
    # The exact restored event supplies the post-turn hook after real dispatch.
    await runner._run_post_turn_hooks(agent_result={'final_response': 'LOOP_COMPLETE'},
                                      source=restored.source, is_internal=True, event=restored)
    assert loops.load_loop(sid).status == 'done'
    assert loops.load_loop(sid).ticks_fired == 1
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_held_transcript_lease_alone_defers_without_unscoped_alias(lane):
    runner, _authority, _adapter, _source, key, sid = lane
    state = await _create(lane)
    held = await runner._turn_leases.acquire(sid, owner_key=key, generation=1)
    try:
        await runner._loop_wakeup_fire_one(sid, state, time.time(), set())
        assert not _automation(lane)
        assert loops.load_loop(sid).ticks_fired == 0
    finally:
        runner._turn_leases.release(held)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('scope_id', 'other-workspace'), ('thread_id', 'other-thread'),
                                        ('profile', 'other-profile'), ('chat_id', 'other-channel'),
                                        ('session_key', 'agent:main:slack:group:wrong'),
                                        ('transport_profile', 'other-profile')])
async def test_wrong_persisted_owner_never_claims_or_dispatches(lane, field, value):
    runner, _authority, adapter, _source, _key, sid = lane
    state = await _create(lane)
    state.route[field] = value
    loops.save_loop(sid, state)
    await runner._loop_wakeup_fire_one(sid, state, time.time(), set())
    assert not _automation(lane)
    assert loops.load_loop(sid).ticks_fired == 0
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_scope_with_ambiguous_owning_routes_refuses(lane):
    runner, _authority, _adapter, source, _key, sid = lane
    state = await _create(lane, legacy=True)
    other = replace_source(source, scope_id='second-workspace', guild_id='second-workspace')
    runner.session_store.get_or_create_session(other)
    runner.session_store.switch_session(runner._session_key_for_source(other), sid)
    await runner._loop_wakeup_fire_one(sid, state, time.time(), set())
    assert not _automation(lane)
    assert loops.load_loop(sid).ticks_fired == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing-origin', 'unavailable-store', 'unavailable-adapter', 'corrupt-restoration'])
async def test_no_owning_evidence_never_infers_scope_from_connected_bot(lane, monkeypatch, failure):
    runner, _authority, adapter, _source, key, sid = lane
    state = await _create(lane, legacy=True)
    if failure == 'missing-origin':
        runner.session_store._entries[key].origin = None
    elif failure == 'unavailable-store':
        monkeypatch.setattr(runner.session_store, 'list_sessions', lambda: (_ for _ in ()).throw(ValueError('unavailable')))
    elif failure == 'corrupt-restoration':
        monkeypatch.setattr(runner, '_restored_source', lambda entry: (_ for _ in ()).throw(ValueError('corrupt')))
    else:
        monkeypatch.setattr(runner, '_adapter_for_source', lambda source: None)
    await runner._loop_wakeup_fire_one(sid, state, time.time(), set())
    assert not _automation(lane)
    assert loops.load_loop(sid).ticks_fired == 0
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_refused_admission_refunds_tick_without_classic_fallback(lane, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    runner, _authority, adapter, _source, _key, sid = lane
    state = await _create(lane)
    refused = AsyncMock(side_effect=RuntimeStoreError('admission_conflict'))
    monkeypatch.setattr('gateway.session_automation.admit_route_wake', refused)
    await runner._loop_wakeup_fire_one(sid, state, time.time(), set())
    refused.assert_awaited_once()
    current = loops.load_loop(sid)
    assert current.ticks_fired == 0 and not current.awaiting_response
    assert current.next_due_at > time.time()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('control', ['pause', 'clear', 'replace'])
async def test_stale_completion_and_refund_cannot_resurrect_controlled_loop(lane, control):
    _runner, _authority, _adapter, _source, _key, sid = lane
    await _create(lane)
    stale = loops.LoopManager(sid)
    assert stale.fire_tick()
    newer = loops.LoopManager(sid)
    if control == 'replace':
        newer.set('replacement fixture', interval_seconds=300)
    else:
        getattr(newer, control)()
    expected = loops.load_loop(sid).to_json()
    stale.complete_tick('still working')
    assert loops.load_loop(sid).to_json() == expected
    stale.abandon_tick()
    assert loops.load_loop(sid).to_json() == expected


@pytest.mark.asyncio
async def test_setup_or_older_tick_cannot_settle_replacement_tick(lane):
    runner, _authority, _adapter, source, _key, sid = lane
    await _create(lane)
    await runner._loop_wakeup_fire_one(sid, loops.load_loop(sid), time.time(), set())
    from gateway.session_envelope import restore_native
    old = restore_native(_automation(lane)[0]['payload'], runner)
    replacement = loops.LoopManager(sid)
    replacement.set('replacement fixture', interval_seconds=300, route=loops.load_loop(sid).route)
    assert replacement.fire_tick()
    expected = loops.load_loop(sid).to_json()
    for event in [MessageEvent(text='setup', source=source), old]:
        await runner._run_post_turn_hooks(agent_result={'final_response': 'LOOP_COMPLETE'},
                                          source=source, is_internal=event.internal, event=event)
        assert loops.load_loop(sid).to_json() == expected


@pytest.mark.asyncio
async def test_control_during_judge_wins_compare_and_swap(lane, monkeypatch):
    _runner, _authority, _adapter, _source, _key, sid = lane
    await _create(lane)
    manager = loops.LoopManager(sid)
    manager.state.until = 'fixture condition'
    manager._save()
    assert manager.fire_tick()
    expected = []

    def judge(*args):
        loops.LoopManager(sid).pause()
        expected.append(loops.load_loop(sid).to_json())
        return 'done', 'fixture done', None, None, None

    monkeypatch.setattr(goals, 'judge_goal', judge)
    result = manager.complete_tick('fixture evidence')
    assert loops.load_loop(sid).to_json() == expected[0]
    assert result['status'] == 'paused' and not result['message']


@pytest.mark.asyncio
async def test_changed_route_owner_during_claim_refuses_original_tick(lane, monkeypatch):
    runner, _authority, adapter, _source, key, sid = lane
    state = await _create(lane)
    original = loops.LoopManager.fire_tick

    def rotate(manager):
        text = original(manager)
        runner.session_store.reset_session(key)
        return text

    monkeypatch.setattr(loops.LoopManager, 'fire_tick', rotate)
    await runner._loop_wakeup_fire_one(sid, state, time.time(), set())
    assert not _automation(lane)
    assert loops.load_loop(sid).ticks_fired == 0
    assert runner.session_store.peek_session_id(key) != sid
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_admitted_tick_executes_and_settles_via_real_ingress(lane, monkeypatch):
    from gateway.session_contract import SessionRef
    from gateway.session_ingress import execute_admission
    from gateway.session_results import execution_result, finish_result
    from hermes_state_runtime import claim_session_input, get_session_admission, settle_session_input

    runner, authority, adapter, _source, key, sid = lane
    state = await _create(lane, native=True)
    await runner._loop_wakeup_fire_one(sid, state, time.time(), set())
    owner = authority.logical_owner(sid)
    # The setup human input has already finished; no model runs in this fixture.
    human = claim_session_input(authority.db, epoch=authority.epoch, session_id=owner)
    settle_session_input(authority.db, epoch=authority.epoch, admission_id=human['admission_id'],
                         generation=human['generation'], outcome='completed')
    row = claim_session_input(authority.db, epoch=authority.epoch, session_id=owner)
    assert row['admission_id'] == _automation(lane)[0]['admission_id']

    async def model_boundary(event):
        assert event.internal and event.source.scope_id == 'workspace'
        assert runner._session_key_for_source(event.source) == key
        assert event.metadata['gateway_proven_session_id'] == sid
        result = {'final_response': 'LOOP_COMPLETE', 'messages': []}
        execution_result.get()['result'] = result
        await runner._run_post_turn_hooks(agent_result=result, source=event.source,
                                          is_internal=True, event=event)
        return result['final_response']

    monkeypatch.setattr(runner, '_handle_message', model_boundary)
    response = await execute_admission(authority, SessionRef(authority.profile_id, owner), row)
    finish_result(authority.db, epoch=authority.epoch, row=row, response=response, outcome='completed',
                   result=authority.pending_results[row['admission_id']])
    settled = get_session_admission(authority.db, admission_id=row['admission_id'])
    assert settled['status'] == 'terminal' and settled['outcome'] == 'completed'
    assert loops.load_loop(sid).status == 'done'
    assert loops.load_loop(sid).ticks_fired == 1
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_scans_can_only_claim_one_tick(lane):
    _runner, _authority, _adapter, _source, _key, sid = lane
    await _create(lane)
    first, second = loops.LoopManager(sid), loops.LoopManager(sid)
    assert first.fire_tick()
    assert second.fire_tick() is None
    assert loops.load_loop(sid).ticks_fired == 1
    assert loops.load_loop(sid).awaiting_response


@pytest.mark.asyncio
async def test_replacement_at_same_clock_instant_has_distinct_tick_identity(lane, monkeypatch):
    _runner, _authority, _adapter, _source, _key, sid = lane
    monkeypatch.setattr(loops.time, 'time', lambda: 1_000_000.0)
    await _create(lane)
    old = loops.LoopManager(sid)
    assert old.fire_tick()
    newer = loops.LoopManager(sid)
    newer.set(old.state.prompt, interval_seconds=300, route=old.state.route)
    assert newer.fire_tick()
    assert newer.state.to_json() != old.state.to_json()
    expected = loops.load_loop(sid).to_json()
    old.complete_tick('LOOP_COMPLETE')
    assert loops.load_loop(sid).to_json() == expected


@pytest.mark.asyncio
async def test_other_receiving_adapter_is_refused_by_canonical_admission(lane, monkeypatch):
    from plugins.platforms.slack.adapter import SlackAdapter
    runner, _authority, adapter, _source, _key, sid = lane
    state = await _create(lane)
    stranger = SlackAdapter(PlatformConfig(enabled=True, token='other-fixture', typing_indicator=False))
    from gateway.loop_routes import resolve_loop_route

    def wrong_adapter(*args):
        restored, key, _adapter = resolve_loop_route(*args)
        return restored, key, stranger

    # Inject a stale/wrong adapter after restoration; retain the authority's real resolver.
    monkeypatch.setattr('gateway.loop_routes.resolve_loop_route', wrong_adapter)
    await runner._loop_wakeup_fire_one(sid, state, time.time(), set())
    assert not _automation(lane)
    assert loops.load_loop(sid).ticks_fired == 0
    adapter.handle_message.assert_not_awaited()
