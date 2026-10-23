"""Managed-directory selection errors must not establish connector credentiallessness."""
import pytest

from gateway.config import Platform
from hermes_cli import managed_scope
from tests.gateway import test_nonmultiplex_cron_routes as routes


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return routes.nonmultiplex.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("selection", ["explicit", "default"])
@pytest.mark.asyncio
async def test_real_managed_parent_traversal_failure_denies_borrowing(rig, tmp_path, monkeypatch, local, selection):
    runner, _, home = rig
    runner._profile_adapters["sat"] = {Platform.LOCAL: object()} if local else {}
    parent = tmp_path / "policy-parent"
    managed = parent / "managed"
    managed.mkdir(parents=True)
    credential = managed / ".env"
    credential.write_text("TELEGRAM_BOT_TOKEN=synthetic-own-B\n")
    if selection == "explicit":
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    else:
        monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
        monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", managed)
        monkeypatch.setattr(managed_scope, "_under_pytest", lambda: False)
    before, eligible = await routes._bootstrap_tick(runner, home, "C1")
    assert not eligible("sat") and before["error"] is not None
    assert runner.adapters[Platform.DISCORD].sent == []
    parent.chmod(0)
    try:
        # Assert actual kernel denial; neither the selector nor eligibility is faked.
        with pytest.raises(PermissionError):
            credential.read_bytes()
        assert managed_scope.get_managed_dir() is None
        after, eligible = await routes._bootstrap_tick(runner, home, "C1")
        assert not eligible("sat")
        assert after["error"] is not None
        assert runner.adapters[Platform.DISCORD].sent == []
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("state", ["missing", "empty", "disabled", "invalid-file", "dangling"])
@pytest.mark.asyncio
async def test_actual_managed_selection_absence_and_invalid_paths(rig, tmp_path, monkeypatch, local, state):
    runner, _, home = rig
    runner._profile_adapters["sat"] = {Platform.LOCAL: object()} if local else {}
    managed = tmp_path / "managed"
    if state == "empty":
        managed.mkdir()
    elif state == "invalid-file":
        managed.write_text("not a directory")
    elif state == "dangling":
        managed.symlink_to(tmp_path / "unavailable-target", target_is_directory=True)
    if state == "disabled":
        monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
        # Existing suite isolation disables implicit system policy, not an invented config switch.
        assert managed_scope._under_pytest()
    else:
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    result, eligible = await routes._bootstrap_tick(runner, home, "C1")
    allowed = state in {"missing", "empty", "disabled"}
    assert eligible("sat") is allowed
    assert (result["error"] is None) is allowed
    assert runner.adapters[Platform.DISCORD].sent == (["C1"] if allowed else [])
