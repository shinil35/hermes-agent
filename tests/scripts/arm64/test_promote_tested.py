"""Promotion validates real data boundaries without a Docker daemon or registry writes."""
import hashlib
import io
import json
import urllib.error
import zipfile

import pytest

from scripts.arm64 import promote_tested as promotion


@pytest.fixture
def metadata():
    run = {"id": promotion.RUN_ID, "run_attempt": 1, "workflow_id": 394103165,
           "head_sha": promotion.BUILD_HEAD, "head_branch": "hermes/arm64-slack-loop-build-0.22.3",
           "event": "push", "status": "completed", "conclusion": "success",
           "path": ".github/workflows/arm64-downstream.yml",
           "repository": {"full_name": promotion.REPOSITORY},
           "head_repository": {"full_name": promotion.REPOSITORY}}
    artifact = {"id": promotion.ARTIFACT_ID, "name": "tested-arm64-40420109138-1",
                "expired": False, "digest": "sha256:" + promotion.ZIP_SHA,
                "workflow_run": {"id": promotion.RUN_ID, "repository_id": 1430649011,
                                 "head_repository_id": 1430649011, "head_sha": promotion.BUILD_HEAD,
                                 "head_branch": "hermes/arm64-slack-loop-build-0.22.3"}}
    return run, artifact


def test_fixed_successful_build_metadata(metadata):
    promotion.validate_metadata(*metadata)


@pytest.mark.parametrize("field,value", [
    ("id", 1), ("run_attempt", 2), ("workflow_id", 1), ("head_sha", promotion.SOURCE),
    ("event", "pull_request"), ("status", "in_progress"), ("conclusion", "failure"),
    ("path", ".github/workflows/other.yml"), ("head_branch", "main"),
    ("head_repository", {"full_name": "other/hermes-agent"}),
])
def test_wrong_or_incomplete_build_denied(metadata, field, value):
    run, artifact = metadata
    run[field] = value
    with pytest.raises(ValueError):
        promotion.validate_metadata(run, artifact)


@pytest.mark.parametrize("field,value", [
    ("id", 1), ("name", "other"), ("expired", True), ("digest", "sha256:" + "0" * 64),
    ("workflow_run", {"id": 1}), ("workflow_run", {"id": promotion.RUN_ID, "head_sha": "0" * 40}),
])
def test_other_or_expired_artifact_denied(metadata, field, value):
    run, artifact = metadata
    artifact[field] = value
    with pytest.raises(ValueError):
        promotion.validate_metadata(run, artifact)


def _archive(tmp_path, monkeypatch, *, failure=None):
    image = b"synthetic compressed tested image"
    image_sha = hashlib.sha256(image).hexdigest()
    revision = "0" * 40 if failure == "source" else promotion.SOURCE
    image_id = "not-an-id" if failure == "config" else "sha256:" + "1" * 64
    archive_sha = "0" * 64 if failure == "archive" else image_sha
    receipt = f"{revision} {image_id} {archive_sha}\n"
    if failure == "oversized":
        receipt += "x" * 4096
    path = tmp_path / "artifact.zip"
    with zipfile.ZipFile(path, "w") as package:
        entry = zipfile.ZipInfo("identity.txt")
        if failure == "symlink":
            entry.external_attr = 0o120777 << 16
        package.writestr(entry, receipt)
        package.writestr("image.tar.gz", image)
        if failure == "layout":
            package.writestr("../escape", b"forbidden")
    if failure != "zip-digest":
        monkeypatch.setattr(promotion, "ZIP_SHA", hashlib.sha256(path.read_bytes()).hexdigest())
    return path, image, image_id


def test_exact_receipt_archive_and_config_identity(tmp_path, monkeypatch):
    path, image, image_id = _archive(tmp_path, monkeypatch)
    result = promotion.unpack_verified(path, tmp_path / "tested")
    assert result == {"revision": promotion.SOURCE, "image_id": image_id,
                      "archive_sha": hashlib.sha256(image).hexdigest()}
    assert (tmp_path / "tested/image.tar.gz").read_bytes() == image


@pytest.mark.parametrize("failure", ["source", "config", "archive", "oversized", "symlink", "layout", "zip-digest"])
def test_changed_or_unsafe_archive_never_loads(tmp_path, monkeypatch, failure):
    path, _, _ = _archive(tmp_path, monkeypatch, failure=failure)
    with pytest.raises(ValueError):
        promotion.unpack_verified(path, tmp_path / "tested")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("status", [200, 401, 403, 404, 500])
def test_only_registry_manifest_absence_allows_new_tag(monkeypatch, status):
    monkeypatch.setenv("GHCR_TOKEN", "synthetic-short-lived-job-token")
    requests = []

    def urlopen(request, timeout):
        requests.append((request.full_url, request.get_method()))
        if len(requests) == 1:
            return io.BytesIO(json.dumps({"token": "synthetic-bearer"}).encode())
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "synthetic", {}, None)
        return io.BytesIO(b"")

    monkeypatch.setattr(promotion.urllib.request, "urlopen", urlopen)
    tag = "ghcr.io/shinil35/hermes-agent:0.22.3-439e5888802a-12345-1"
    if status == 404:
        promotion.require_unused_tag(tag)
    else:
        with pytest.raises((ValueError, RuntimeError)):
            promotion.require_unused_tag(tag)
    assert len(requests) == 2 and requests[1][1] == "HEAD"
    assert requests[1][0].startswith("https://ghcr.io/v2/shinil35/hermes-agent/manifests/")


def test_other_registry_or_tag_is_not_a_publication_target(monkeypatch):
    def network_forbidden(*args, **kwargs):
        raise AssertionError("invalid target must fail before network")

    monkeypatch.setattr(promotion.urllib.request, "urlopen", network_forbidden)
    with pytest.raises(ValueError):
        promotion.require_unused_tag("ghcr.io/other/hermes-agent:latest")
