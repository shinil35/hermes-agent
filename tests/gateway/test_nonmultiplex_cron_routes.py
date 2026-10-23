"""R1: real bootstrap must keep routed cron independent of adapter multiplexing."""
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
import hermes_yaml as yaml

import gateway.run as gateway_run
from cron.scheduler import _deliver_result
from gateway.config import Platform, load_gateway_config
from gateway.run import _profile_runtime_scope
from gateway.status import GatewayLiveness
from tests.gateway import test_local_satellite_cron as satellite


@pytest.fixture
def nonmultiplex(tmp_path, monkeypatch):
    runner, root, home = satellite.rig.__wrapped__(tmp_path, monkeypatch)
    config_path = root / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
    config["gateway"]["multiplex_profiles"] = False
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with _profile_runtime_scope(root):
        runner.config = load_gateway_config()
    # Real nonmultiplex startup never populates secondary adapter configs.
    runner._profile_configs = {}
    runner._draining = runner._external_drain_active = False
    monkeypatch.setattr("gateway.status.resolve_gateway_liveness", lambda **kw: GatewayLiveness(
        running=False, pid=None, source="none"))
    return runner, root, home


async def _bootstrap_tick(runner, home, chat):
    """Capture actual bootstrap arguments, then run its actual provider on the live loop."""
    captured = {}

    class CaptureTicker:
        def __init__(self, target, *, args, kwargs, stop_event):
            captured.update(target=target, args=args, kwargs=kwargs)

    with patch("cron.scheduler_thread.SupervisedTickerThread", CaptureTicker), \
            patch("gateway.runtime_ownership.process_ownership.start_writer"):
        stop, provider, _, _ = gateway_run._start_gateway_start_cron_and_housekeeping(runner)
    result = {}

    def tick(**kwargs):
        from hermes_constants import get_hermes_home
        if Path(get_hermes_home()) != home:
            return
        try:
            result["adapters"] = kwargs["adapters"]
            result["error"] = _deliver_result({"id": "r1", "deliver": f"discord:{chat}"}, "hello",
                                              adapters=kwargs["adapters"], loop=kwargs["loop"])
        finally:
            stop.set()

    kwargs = {**captured["kwargs"], "interval": 0}
    try:
        # No real standalone network is ever allowed by this probe.
        with patch("cron.scheduler.tick", side_effect=tick), \
                patch("tools.send_message_tool._send_to_platform",
                      side_effect=AssertionError("unexpected standalone network")):
            await asyncio.wait_for(asyncio.to_thread(provider.start, *captured["args"], **kwargs), timeout=10)
    finally:
        stop.set()
    assert "error" in result, "actual bootstrap must still tick the secondary home"
    return result, captured["kwargs"]["shared_route_eligible"]


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("chat,allowed", [("C1", True), ("C2", False), ("OFF", False), ("unmapped", False)])
@pytest.mark.asyncio
async def test_bootstrap_nonmultiplex_exact_route_delivery(nonmultiplex, local, chat, allowed):
    runner, _, home = nonmultiplex
    own = {Platform.LOCAL: object()} if local else {}
    runner._profile_adapters["sat"] = own
    result, eligible = await _bootstrap_tick(runner, home, chat)
    assert not runner.config.multiplex_profiles
    assert not runner._is_shared_bot_satellite("sat"), "gateway satellite semantics must not change"
    assert runner._profile_adapters["sat"] is own
    if allowed:
        assert result["error"] is None
        assert runner.adapters[Platform.DISCORD].sent == ["C1"]
        assert eligible("sat")
    else:
        assert result["error"] is not None
        assert runner.adapters[Platform.DISCORD].sent == []
    assert not eligible("missing")


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("state", ["connected", "retryable", "terminal"])
@pytest.mark.asyncio
async def test_nonmultiplex_actual_own_connector_isolation(nonmultiplex, local, state):
    runner, _, home = nonmultiplex
    (home / "config.yaml").write_text(yaml.safe_dump({"platforms": {
        "telegram": {"enabled": True, "token": "synthetic-own-B"}}}), encoding="utf-8")
    own = {Platform.LOCAL: object()} if local else {}
    runner._profile_adapters["sat"] = own
    bot = satellite.FakeBot(retryable=state == "retryable")

    def create(platform, config):
        assert platform is Platform.TELEGRAM and config.token == "synthetic-own-B"
        bot.config = config
        return bot

    async def connect(adapter, platform):
        await adapter.connect()
        return state == "connected"

    runner._create_adapter = create
    runner._connect_initial_adapter_with_timeout = connect
    try:
        assert await runner._start_one_profile_adapters("sat", home, {}) == (state == "connected")
        await asyncio.sleep(0)
        assert bot.owner == "sat" and bot.connected.is_set()
        if state == "retryable":
            assert runner._profile_failed_platforms["sat"]
        else:
            assert not runner._profile_failed_platforms
        runner._profile_configs.clear()  # Nonmultiplex eligibility must not need startup caches.
        result, eligible = await _bootstrap_tick(runner, home, "C1")
        assert result["adapters"] == own
        assert result["error"] is not None
        assert runner.adapters[Platform.DISCORD].sent == []
        assert not eligible("sat")
    finally:
        tasks = list(runner._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.asyncio
async def test_nonmultiplex_reads_uncached_own_connector_config(nonmultiplex, enabled):
    runner, _, home = nonmultiplex
    (home / "config.yaml").write_text(yaml.safe_dump({"platforms": {
        "telegram": {"enabled": enabled, "token": "synthetic-own-B"}}}), encoding="utf-8")
    assert not runner._profile_configs
    result, eligible = await _bootstrap_tick(runner, home, "C1")
    assert eligible("sat") is not enabled
    assert (result["error"] is None) is not enabled
    assert runner.adapters[Platform.DISCORD].sent == ([] if enabled else ["C1"])


@pytest.mark.asyncio
async def test_nonmultiplex_listener_is_not_a_shared_mirror(nonmultiplex):
    runner, _, home = nonmultiplex
    (home / "config.yaml").write_text(yaml.safe_dump({"platforms": {
        "webhook": {"enabled": True}}}), encoding="utf-8")
    with _profile_runtime_scope(home):
        config = load_gateway_config()
    assert config.platforms[Platform.WEBHOOK].enabled
    result, eligible = await _bootstrap_tick(runner, home, "C1")
    assert not eligible("sat")
    assert result["error"] is not None
    assert runner.adapters[Platform.DISCORD].sent == []


@pytest.mark.asyncio
async def test_scoped_cron_callback_cannot_reintroduce_unserved_home(nonmultiplex):
    runner, root, home = nonmultiplex
    result, eligible = await _bootstrap_tick(runner, home, "C1")
    assert result["error"] is None
    runner.config._runtime_profile_homes = [("default", root)]
    # Eligibility runs inside the secondary scope, so launch-home inference must not
    # add the just-removed home back into the actual served reservation.
    with _profile_runtime_scope(home):
        assert not eligible("sat")
