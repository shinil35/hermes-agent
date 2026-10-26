"""Secret-free core smoke run inside the image as its service UID.

Browser automation is excluded: presence is diagnostic, never a navigation pass.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys


def check_stamp(stamp: dict, revision: str) -> None:
    assert stamp["schemaVersion"] == 2
    assert stamp["commit"] == revision
    assert stamp["baseVersion"] == "0.22.3"
    assert stamp["distribution"] == "docker"
    assert stamp["updateMechanism"] == "external"
    assert stamp["source"] == "ci"
    assert not stamp["dirty"]


def main() -> None:
    getuid = getattr(os, "getuid", None)
    assert getuid is not None and getuid() == 10000, "core checks must run as service UID"
    assert platform.machine() in {"aarch64", "arm64"}
    root = Path("/opt/hermes")
    check_stamp(json.loads((root / "install-stamp.json").read_text(encoding="utf-8-sig")), sys.argv[1])
    for name in ("gateway.config", "gateway.loop_routes", "hermes_cli.loops_state",
                 "cron.scheduler_preflight", "hermes_cli.version_info"):
        importlib.import_module(name)
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute("select 2+2").fetchone()[0] == 4
    for directory in (root, root / ".venv", root / "pm-runtime", root / "tools"):
        assert not os.access(directory, os.W_OK), f"sealed path writable: {directory}"
    state = Path("/opt/data/arm64-smoke-state")
    if sys.argv[2] == "2":
        assert state.exists(), "state did not survive container recreation"
    if state.exists():
        assert state.read_text(encoding="utf-8-sig") == "persistent"
    state.write_text("persistent", encoding="utf-8")
    # Reading PM facts is not an install, driver launch or browser acceptance.
    facts = json.loads((root / "tools/facts.json").read_text(encoding="utf-8-sig"))
    print(json.dumps({"core": "passed", "uid": getuid(),
                      "browser": "EXCLUDED: no navigation/extraction test or helper repair",
                      "agent_browser_fact_present": "agent-browser" in facts.get("packages", {}),
                      "upstream_image_marker": json.loads(Path("/etc/hermes/image-provenance.json").read_text(encoding="utf-8-sig"))["image"]}))


if __name__ == "__main__":
    main()
