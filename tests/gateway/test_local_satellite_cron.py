"""LOCAL satellite delivery, adapted from liuhao1024's PR #139960.

Exercise actual scoped configuration, startup ownership and durable delivery;
only transport I/O and unrelated plugin/lifecycle hooks are faked.
"""
import asyncio
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import hermes_yaml as yaml

import gateway.run as gateway_run
from cron import delivery_queue
from cron.scheduler import _deliver_result
from gateway.config import Platform, load_gateway_config
from gateway.run import GatewayRunner, _profile_runtime_scope


class FakeBot:
    def __init__(self, config=None, *, retryable=False):
        self.config = config
        self.fatal_error_retryable = retryable
        self.fatal_error_code = None
        self.sent = []
        self.owner = None
        self.connected = threading.Event()

    def set_owner_profile(self, profile):
        self.owner = profile

    async def connect(self):
        self.connected.set()
        return False

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, metadata=None):
        self.sent.append(chat_id)
        return {"success": True, "message_id": "sent-1"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    root = tmp_path / "home"
    home = root / "profiles" / "sat"
    other = root / "profiles" / "other"
    for directory in (home, other):
        (directory / "cron").mkdir(parents=True)
        (directory / "config.yaml").write_text("platforms: {}\n", encoding="utf-8")
    (root / "config.yaml").write_text(yaml.safe_dump({
        "gateway": {"multiplex_profiles": True, "profile_routes": [
            {"platform": "discord", "guild_id": "G1", "chat_id": "C1", "profile": "sat"},
            {"platform": "discord", "chat_id": "C2", "profile": "other"},
            {"platform": "discord", "chat_id": "OFF", "profile": "sat", "enabled": False},
        ]},
        "platforms": {"discord": {"enabled": True, "token": "synthetic-primary-A"}},
    }), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda *a, **k: [
        ("default", root), ("sat", home), ("other", other)])
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: root / "profiles" / name)
    with _profile_runtime_scope(root):
        config = load_gateway_config()
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner._primary_profile_name = "default"
    runner.adapters = {Platform.DISCORD: FakeBot(config.platforms[Platform.DISCORD])}
    runner._profile_adapters = {"sat": {}, "other": {}}
    runner._profile_configs = {}
    runner._profile_failed_platforms = {}
    runner._running = True
    runner._busy_text_mode = "queue"
    runner._background_tasks = set()
    runner._shutdown_event = asyncio.Event()
    # No plugin install, network hydration or host-wide hooks in this probe.
    monkeypatch.setattr("gateway.run_startup.recover_left_core_in", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda *a, **k: None)
    runner._subscribe_plugin_rewire = lambda *a: None
    runner._register_config_hooks = lambda *a, **k: None
    runner._snapshot_profile_busy_modes = lambda *a: None
    runner._wire_adapter_handlers = lambda *a, **k: None
    runner._bind_voice_input_callback = lambda *a: None
    runner._sync_voice_mode_state_to_adapter = lambda *a: None
    runner._update_platform_runtime_status = lambda *a, **k: None
    with _profile_runtime_scope(home):
        runner._profile_configs["sat"] = load_gateway_config()
    with _profile_runtime_scope(other):
        runner._profile_configs["other"] = load_gateway_config()
    return runner, root, home


@pytest.mark.parametrize("local", [False, True])
def test_local_satellite_resolution_and_local_output(rig, local):
    runner, _, home = rig
    local_adapter = object()
    own = {Platform.LOCAL: local_adapter} if local else {}
    runner._profile_adapters["sat"] = own
    resolved = runner._adapters_for_profile("sat")
    assert resolved[Platform.DISCORD] is runner.adapters[Platform.DISCORD]
    if local:
        assert resolved[Platform.LOCAL] is local_adapter
    assert runner._profile_adapters["sat"] is own
    with _profile_runtime_scope(home):
        assert _deliver_result({"id": "local", "deliver": "local"}, "retained", adapters=own) is None
    assert runner._adapters_for_profile("missing") == {}
    assert runner._adapters_for_profile("default") is runner.adapters


@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize("local", [False, True])
@pytest.mark.asyncio
async def test_failed_real_own_connector_never_borrows(rig, retryable, local):
    runner, _, home = rig
    # Distinct own token; a platform key without this token is NOT an owner.
    (home / "config.yaml").write_text(yaml.safe_dump({"platforms": {
        "telegram": {"enabled": True, "token": "synthetic-own-B"}}}), encoding="utf-8")
    bot = FakeBot(retryable=retryable)
    def create(platform, config):
        assert platform is Platform.TELEGRAM
        assert config.token == "synthetic-own-B"
        bot.config = config
        return bot
    runner._create_adapter = create
    runner._connect_initial_adapter_with_timeout = lambda adapter, platform: adapter.connect()
    own = {Platform.LOCAL: object()} if local else {}
    runner._profile_adapters["sat"] = own
    try:
        assert await runner._start_one_profile_adapters("sat", home, {}) == 0
        # Let the production cold-start handoff register the retry task.
        await asyncio.sleep(0)
        assert bot.connected.is_set() and bot.owner == "sat"
        assert bot._hermes_profile_name == "sat"
        assert runner._profile_configs["sat"].platforms[Platform.TELEGRAM].token == "synthetic-own-B"
        if retryable:
            assert runner._profile_failed_platforms["sat"]
        else:
            assert not runner._profile_failed_platforms
        assert runner._adapters_for_profile("sat") is own
        assert _tick_view(runner, home) == own
        with _profile_runtime_scope(home):
            delivery_queue.enqueue("failed-own", {"id": "j", "deliver": "discord:C1"}, "hello")
        with patch.object(gateway_run, "_handoff_watch_scopes", return_value=[("sat", home)]):
            _drain_on_live_loop(runner)
        assert runner.adapters[Platform.DISCORD].sent == []
        with _profile_runtime_scope(home):
            assert delivery_queue.get_status("failed-own")["status"] != "delivered"
    finally:
        tasks = list(runner._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("local", [False, True])
def test_recreated_runner_drains_queue_once_through_scoped_route(rig, monkeypatch, local):
    previous, root, home = rig
    with _profile_runtime_scope(home):
        delivery_queue.enqueue("execution", {"id": "job", "deliver": "discord:C1"}, "hello")
    # A fresh runner reads config and queue from disk; no transport object survives.
    runner = object.__new__(GatewayRunner)
    with _profile_runtime_scope(root):
        runner.config = load_gateway_config()
    with _profile_runtime_scope(home):
        runner._profile_configs = {"sat": load_gateway_config()}
    runner.adapters = {Platform.DISCORD: FakeBot(runner.config.platforms[Platform.DISCORD])}
    runner._profile_failed_platforms = {}
    own = {Platform.LOCAL: object()} if local else {}
    runner._profile_adapters = {"sat": own}
    monkeypatch.setattr(gateway_run, "_handoff_watch_scopes", lambda r: [("sat", home)])
    _drain_on_live_loop(runner)
    _drain_on_live_loop(runner)
    with _profile_runtime_scope(home):
        assert delivery_queue.get_status("execution")["status"] == "delivered"
    assert runner.adapters[Platform.DISCORD].sent == ["C1"]
    assert previous.adapters[Platform.DISCORD].sent == []
    assert runner._profile_adapters["sat"] is own


def _tick_view(runner, home):
    from cron.scheduler_provider import InProcessCronScheduler
    captured = []
    stop = threading.Event()

    def tick(**kwargs):
        captured.append(kwargs["adapters"])
        stop.set()

    with patch("cron.scheduler.tick", side_effect=tick):
        thread = threading.Thread(target=InProcessCronScheduler().start, args=(stop,), kwargs={
            "interval": 0, "profile_homes": [("sat", home)],
            "adapters": runner.adapters, "profile_adapters": runner._profile_adapters,
            "default_profile": "default", "shared_route_eligible": runner._is_shared_bot_satellite,
        })
        thread.start()
        thread.join(timeout=10)
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize("local", [False, True])
def test_ticker_real_route_view_and_own_bot_boundary(rig, local):
    from cron.scheduler_preflight import SharedRouteAdapters
    from tests.cron.test_cron_multiplex_shared_route_delivery import _run

    runner, _, home = rig
    own = {Platform.LOCAL: object()} if local else {}
    runner._profile_adapters["sat"] = own
    shared = _tick_view(runner, home)
    assert isinstance(shared, SharedRouteAdapters)
    with _profile_runtime_scope(home):
        error, standalone = _run({"id": "tick", "deliver": "discord:C1"}, shared)
    assert error is None and standalone == []
    assert runner.adapters[Platform.DISCORD].sent == ["C1"]
    for chat in ("C2", "OFF", "unmapped"):
        assert shared.get(Platform.DISCORD, {"chat_id": chat}) is None
    assert shared.get(Platform.TELEGRAM, {"chat_id": "C1"}) is None
    assert shared.get(Platform.DISCORD) is None
    assert runner._profile_adapters["sat"] is own
    own[Platform.TELEGRAM] = FakeBot()
    assert runner._adapters_for_profile("sat") is own
    assert _tick_view(runner, home) == own


@pytest.mark.parametrize("platform,block", [
    ("telegram", {"enabled": True}),
    ("telegram", {"enabled": False, "token": "not-enabled"}),
    ("api_server", {"enabled": True}),
    ("relay", {"enabled": True}),
])
def test_configured_blocks_alone_do_not_claim_a_bot(rig, platform, block):
    runner, _, home = rig
    (home / "config.yaml").write_text(yaml.safe_dump({"platforms": {platform: block}}), encoding="utf-8")
    with _profile_runtime_scope(home):
        runner._profile_configs["sat"] = load_gateway_config()
    runner._profile_adapters["sat"] = {Platform.LOCAL: object()}
    assert runner._adapters_for_profile("sat")[Platform.DISCORD] is runner.adapters[Platform.DISCORD]


def test_new_local_sharing_requires_served_route_and_known_config(rig):
    runner, _, _ = rig
    own = {Platform.LOCAL: object()}
    runner._profile_adapters["sat"] = own
    runner._profile_configs.pop("sat")
    assert runner._adapters_for_profile("sat") is own
    runner._profile_configs["sat"] = load_gateway_config()
    runner.config.profile_routes = []
    assert runner._adapters_for_profile("sat") is own
    runner.config.multiplex_profiles = False
    assert runner._adapters_for_profile("sat") is own


def _drain_on_live_loop(runner):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    try:
        with patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}):
            gateway_run._drain_restart_safe_cron_deliveries(runner.adapters, loop, runner)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


@pytest.mark.parametrize("adapters,expected", [
    ({}, False), (None, False), ({Platform.LOCAL: object()}, False),
    ({"LOCAL": object()}, False), ({Platform.LOCAL: object(), Platform.TELEGRAM: object()}, True),
    ({"plugin-platform": object()}, True),
])
def test_only_local_is_exempt_from_map_ownership(adapters, expected):
    from cron.scheduler_preflight import has_credential_platform
    assert has_credential_platform(adapters) is expected


def test_unserved_profile_cannot_borrow(rig):
    runner, _, _ = rig
    own = {Platform.LOCAL: object()}
    runner._profile_adapters["unserved"] = own
    runner._profile_configs["unserved"] = runner._profile_configs["sat"]
    from gateway.profile_routing import parse_profile_routes
    runner.config.profile_routes.extend(parse_profile_routes([
        {"platform": "discord", "chat_id": "C1", "profile": "unserved"}]))
    assert runner._adapters_for_profile("unserved") is own
