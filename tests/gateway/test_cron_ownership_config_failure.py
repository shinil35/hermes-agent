"""R2: a forgiving configuration fallback must never grant shared-bot ownership."""
import asyncio
import builtins
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import hermes_yaml as yaml

from gateway.config import Platform
from tests.gateway import test_nonmultiplex_cron_routes as routes
from tests.gateway.test_local_satellite_cron import FakeBot


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return routes.nonmultiplex.__wrapped__(tmp_path, monkeypatch)


async def _terminal_owner(runner, home):
    (home / "config.yaml").write_text(yaml.safe_dump({"platforms": {
        "telegram": {"enabled": True, "token": "synthetic-own-B"}}}), encoding="utf-8")
    bot = FakeBot(retryable=False)

    def create(platform, config):
        assert platform is Platform.TELEGRAM and config.token == "synthetic-own-B"
        bot.config = config
        return bot

    runner._create_adapter = create
    runner._connect_initial_adapter_with_timeout = lambda adapter, platform: adapter.connect()
    assert await runner._start_one_profile_adapters("sat", home, {}) == 0
    await asyncio.sleep(0)
    assert bot.connected.is_set() and bot.owner == "sat"
    assert not runner._profile_failed_platforms
    return runner._profile_configs["sat"]


@contextmanager
def _broken_file(path, failure):
    if failure == "missing":
        path.unlink()
    elif failure != "unreadable":
        path.write_text({"malformed": "platforms: [\n", "wrong-root": "- telegram\n",
                         "empty-list": "[]\n", "false-root": "false\n",
                         "wrong-platforms": "platforms: []\n",
                         "wrong-block": "platforms: {telegram: []}\n"}[failure], encoding="utf-8")
    original_open = builtins.open

    def open_file(file, mode="r", *args, **kwargs):
        if failure == "unreadable" and Path(file) == path and "r" in mode:
            raise PermissionError("synthetic EACCES for disposable ownership config")
        return original_open(file, mode, *args, **kwargs)

    with patch("builtins.open", side_effect=open_file):
        yield


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("failure", ["malformed", "wrong-root", "empty-list", "false-root", "missing", "unreadable"])
@pytest.mark.asyncio
async def test_terminal_owner_cannot_be_erased_by_forgiving_reload(rig, local, failure):
    runner, _, home = rig
    runner._profile_adapters["sat"] = {Platform.LOCAL: object()} if local else {}
    cached = await _terminal_owner(runner, home)
    before, eligible = await routes._bootstrap_tick(runner, home, "C1")
    assert before["error"] is not None and not eligible("sat")
    with _broken_file(home / "config.yaml", failure):
        after, eligible = await routes._bootstrap_tick(runner, home, "C1")
        assert not eligible("sat")
        assert after["error"] is not None
    assert runner._profile_configs["sat"] is cached
    assert runner.adapters[Platform.DISCORD].sent == []


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("failure", ["malformed", "wrong-root", "empty-list", "false-root", "unreadable",
                                    "wrong-platforms", "wrong-block"])
@pytest.mark.asyncio
async def test_uncached_config_failure_is_unknown_not_credentialless(rig, local, failure):
    runner, _, home = rig
    runner._profile_adapters["sat"] = {Platform.LOCAL: object()} if local else {}
    assert not runner._profile_configs
    with _broken_file(home / "config.yaml", failure):
        after, eligible = await routes._bootstrap_tick(runner, home, "C1")
        assert not eligible("sat")
        assert after["error"] is not None
    assert runner.adapters[Platform.DISCORD].sent == []


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("contents", [None, "", "{}\n", "platforms: {}\n", "null\n"])
@pytest.mark.asyncio
async def test_uncached_reliably_empty_or_absent_config_keeps_route(rig, contents, local):
    runner, _, home = rig
    runner._profile_adapters["sat"] = {Platform.LOCAL: object()} if local else {}
    path = home / "config.yaml"
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents, encoding="utf-8")
    result, eligible = await routes._bootstrap_tick(runner, home, "C1")
    assert eligible("sat")
    assert result["error"] is None
    assert runner.adapters[Platform.DISCORD].sent == ["C1"]


@pytest.mark.parametrize("layer", ["legacy", "managed"])
@pytest.mark.parametrize("contents", ["owned", "malformed", "wrong-root", "empty"])
@pytest.mark.asyncio
async def test_uncached_other_gateway_layers_cannot_hide_ownership(rig, tmp_path, monkeypatch, layer, contents):
    import json
    runner, _, home = rig
    managed = tmp_path / "managed"
    managed.mkdir()
    if layer == "managed":
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    path = home / "gateway.json" if layer == "legacy" else managed / "config.yaml"
    data = {"platforms": {"telegram": {"enabled": True, "token": "synthetic-own-B"}}}
    if contents == "owned":
        text = json.dumps(data) if layer == "legacy" else yaml.safe_dump(data)
    elif contents == "malformed":
        text = "{broken" if layer == "legacy" else "platforms: [\n"
    elif contents == "wrong-root":
        text = '["telegram"]'
    else:
        text = "{}"
    path.write_text(text, encoding="utf-8")
    result, eligible = await routes._bootstrap_tick(runner, home, "C1")
    allowed = contents == "empty"
    assert eligible("sat") is allowed
    assert (result["error"] is None) is allowed
    assert runner.adapters[Platform.DISCORD].sent == (["C1"] if allowed else [])


def test_validated_snapshot_is_used_after_file_changes(rig):
    from gateway import config_loader
    from gateway.run import _profile_runtime_scope
    runner, _, home = rig
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump({"platforms": {
        "telegram": {"enabled": True, "token": "synthetic-own-B"}}}), encoding="utf-8")
    bridge = config_loader.load_yaml_layer

    def edit_before_bridge(*args, **kwargs):
        path.write_text("platforms: [\n", encoding="utf-8")
        return bridge(*args, **kwargs)

    with _profile_runtime_scope(home), patch("gateway.config_loader.load_yaml_layer", side_effect=edit_before_bridge):
        config = config_loader.load_ownership_config(home)
    assert config.platforms[Platform.TELEGRAM].token == "synthetic-own-B"
    assert runner.adapters[Platform.DISCORD].sent == []


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.asyncio
async def test_cached_credentiallessness_is_not_permission_after_read_failure(rig, local):
    from gateway.config import load_gateway_config
    from gateway.run import _profile_runtime_scope
    runner, _, home = rig
    runner._profile_adapters["sat"] = {Platform.LOCAL: object()} if local else {}
    with _profile_runtime_scope(home):
        runner._profile_configs["sat"] = load_gateway_config()
    (home / "config.yaml").write_text("platforms: [\n", encoding="utf-8")
    result, eligible = await routes._bootstrap_tick(runner, home, "C1")
    assert not eligible("sat")
    assert result["error"] is not None
    assert runner.adapters[Platform.DISCORD].sent == []
