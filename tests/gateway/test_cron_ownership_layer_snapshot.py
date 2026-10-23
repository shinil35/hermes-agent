"""Credential read failures and managed/env rereads cannot authorize bot borrowing."""
import builtins
from pathlib import Path
from unittest.mock import patch

import pytest
import hermes_yaml as yaml

from gateway import config_loader
from gateway.config import Platform
from tests.gateway import test_cron_ownership_config_failure as ownership
from tests.gateway import test_nonmultiplex_cron_routes as routes


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return ownership.rig.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("layer", ["user", "managed"])
@pytest.mark.parametrize("state", ["owned", "unreadable", "absent", "empty", "dangling"])
@pytest.mark.asyncio
async def test_env_layer_must_be_reliably_read(rig, tmp_path, monkeypatch, local, layer, state):
    runner, _, home = rig
    runner._profile_adapters["sat"] = {Platform.LOCAL: object()} if local else {}
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    path = (home if layer == "user" else managed) / ".env"
    if state == "dangling":
        path.symlink_to(tmp_path / "missing-secret")
    elif state != "absent":
        path.write_text("" if state == "empty" else "TELEGRAM_BOT_TOKEN=synthetic-own-B\n")
    original = builtins.open

    def open_file(file, *args, **kwargs):
        if state == "unreadable" and Path(file) == path:
            raise PermissionError("synthetic EACCES for disposable credential layer")
        return original(file, *args, **kwargs)

    with patch("builtins.open", side_effect=open_file):
        result, eligible = await routes._bootstrap_tick(runner, home, "C1")
        allowed = state in {"absent", "empty"}
        assert eligible("sat") is allowed
        assert (result["error"] is None) is allowed
    assert runner.adapters[Platform.DISCORD].sent == (["C1"] if allowed else [])


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("layer", ["managed-yaml", "user-env", "managed-env"])
@pytest.mark.asyncio
async def test_validated_connector_snapshot_survives_layer_mutation(rig, tmp_path, monkeypatch, local, layer):
    runner, _, home = rig
    runner._profile_adapters["sat"] = {Platform.LOCAL: object()} if local else {}
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    if layer == "managed-yaml":
        path = managed / "config.yaml"
        data = {"platforms": {"matrix": {"enabled": True, "extra": {
            "homeserver": "https://synthetic.invalid", "user_id": "@own:synthetic.invalid",
            "password": "synthetic-own-B"}}}}
        path.write_text(yaml.safe_dump(data))
        replacement = "platforms: {matrix: {enabled: true, extra: {password: ''}}}\n"
    else:
        path = (home if layer == "user-env" else managed) / ".env"
        path.write_text("TELEGRAM_BOT_TOKEN=synthetic-own-B\n")
        replacement = ""
    bridge = config_loader.load_yaml_layer

    def edit_before_bridge(*args, **kwargs):
        path.write_text(replacement)
        return bridge(*args, **kwargs)

    # Each callback is a fresh resolution; restore the owned state for its read.
    original = path.read_text()
    with patch("gateway.config_loader.load_yaml_layer", side_effect=edit_before_bridge):
        result, eligible = await routes._bootstrap_tick(runner, home, "C1")
        path.write_text(original)
        assert not eligible("sat")
    assert result["error"] is not None
    assert runner.adapters[Platform.DISCORD].sent == []


@pytest.mark.parametrize("layer", ["user-env", "managed-env", "managed-yaml"])
def test_ownership_bridge_does_not_reread_credential_layers(rig, tmp_path, monkeypatch, layer):
    from agent.secret_scope import current_secret_scope
    from gateway.run import _profile_runtime_scope
    runner, _, home = rig
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    path = managed / "config.yaml" if layer == "managed-yaml" else (
        home if layer == "user-env" else managed) / ".env"
    path.write_text("platforms: {telegram: {enabled: true, token: synthetic-own-B}}\n"
                    if layer == "managed-yaml" else "TELEGRAM_BOT_TOKEN=synthetic-own-B\n")
    original = builtins.open
    reads = 0

    def open_once(file, *args, **kwargs):
        nonlocal reads
        if Path(file) == path:
            reads += 1
            if reads > 1:
                raise PermissionError("disposable layer became unavailable after snapshot")
        return original(file, *args, **kwargs)

    import os
    ambient = dict(os.environ)
    with _profile_runtime_scope(home, prepared_secret_scope={}), patch("builtins.open", side_effect=open_once):
        prior = current_secret_scope()
        config = config_loader.load_ownership_config(home)
        assert current_secret_scope() is prior
    assert reads == 1
    assert config.platforms[Platform.TELEGRAM].token == "synthetic-own-B"
    assert dict(os.environ) == ambient
    assert runner._profile_has_own_connector("sat", home, config)


def test_ownership_yaml_probe_preserves_ambient_and_nonmultiplex_connector_mode(rig, monkeypatch):
    import os
    from agent.secret_scope import is_multiplex_active, reset_multiplex_context, set_multiplex_context
    from gateway.run import _profile_runtime_scope
    runner, _, home = rig
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("MATRIX_PASSWORD", raising=False)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "allow_all_users": True,
        "matrix": {"homeserver": "https://synthetic.invalid", "user_id": "@own:synthetic.invalid",
                   "password": "synthetic-own-B"},
    }))
    # API_SERVER_KEY is an own listener in nonmultiplex mode, never a listener mirror.
    (home / ".env").write_text("API_SERVER_KEY=synthetic-own-listener-key-B\n")
    token = set_multiplex_context(False)
    ambient = dict(os.environ)
    try:
        with _profile_runtime_scope(home, prepared_secret_scope={}):
            config = config_loader.load_ownership_config(home)
            assert not is_multiplex_active()
        assert dict(os.environ) == ambient
        assert config.platforms[Platform.API_SERVER].enabled
        assert config.platforms[Platform.MATRIX].extra["password"] == "synthetic-own-B"
        assert runner._profile_has_own_connector("sat", home, config)
    finally:
        reset_multiplex_context(token)
