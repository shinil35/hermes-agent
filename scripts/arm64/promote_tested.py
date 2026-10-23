"""Verify the one approved ARM64 build artifact before promoting its unchanged image."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import urllib.error
import urllib.request
import zipfile

REPOSITORY = "shinil35/hermes-agent"
SOURCE = "d5609112e083da74d31803e1b248bb02a33f342d"
BUILD_HEAD = "6d3a56cf4ae7b50026045368cbe69b1be0e4b63f"
RUN_ID = 39965914486
ARTIFACT_ID = 12506923397
ZIP_SHA = "c35b3de72a82fdd68680be2b4bdf8fa0b8cfe4cb7c897af2167d1449fa1a8092"


def _match(actual, expected):
    for key, value in expected.items():
        if actual.get(key) != value:
            raise ValueError(f"Approved artifact metadata mismatch: {key}")


def validate_metadata(run, artifact):
    _match(run, {"id": RUN_ID, "run_attempt": 1, "workflow_id": 394103165,
                 "head_sha": BUILD_HEAD, "head_branch": "hermes/arm64-packaging",
                 "event": "pull_request", "status": "completed", "conclusion": "success",
                 "path": ".github/workflows/arm64-downstream.yml"})
    for key in ("repository", "head_repository"):
        _match(run.get(key, {}), {"full_name": REPOSITORY})
    _match(artifact, {"id": ARTIFACT_ID, "name": "tested-arm64-39965914486-1",
                      "expired": False, "digest": "sha256:" + ZIP_SHA})
    _match(artifact.get("workflow_run", {}), {
        "id": RUN_ID, "repository_id": 1430649011, "head_repository_id": 1430649011,
        "head_sha": BUILD_HEAD, "head_branch": "hermes/arm64-packaging"})


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unpack_verified(archive, destination):
    if _digest(archive) != ZIP_SHA:
        raise ValueError("Approved artifact ZIP digest mismatch")
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) != 2 or {e.filename for e in entries} != {"identity.txt", "image.tar.gz"}:
            raise ValueError("Unexpected artifact layout")
        for entry in entries:
            if entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Encrypted or linked artifact member")
        if package.getinfo("identity.txt").file_size > 4096:
            raise ValueError("Oversized image receipt")
        receipt = package.read("identity.txt").decode("ascii")
        fields = receipt.split()
        if len(fields) != 3:
            raise ValueError("Invalid image receipt")
        revision, image_id, archive_sha = fields
        if (revision != SOURCE or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
                or not re.fullmatch(r"[0-9a-f]{64}", archive_sha)
                or receipt != f"{revision} {image_id} {archive_sha}\n"):
            raise ValueError("Unapproved image receipt")
        if package.getinfo("image.tar.gz").file_size > 8 * 1024**3:
            raise ValueError("Oversized tested image archive")
        destination.mkdir()
        (destination / "identity.txt").write_text(receipt, encoding="ascii")
        with package.open("image.tar.gz") as source, (destination / "image.tar.gz").open("wb") as target:
            shutil.copyfileobj(source, target)
    if _digest(destination / "image.tar.gz") != archive_sha:
        raise ValueError("Tested image archive digest mismatch")
    return {"revision": revision, "image_id": image_id, "archive_sha": archive_sha}


def require_unused_tag(tag):
    if not re.fullmatch(r"ghcr.io/shinil35/hermes-agent:0\.22\.3-d5609112e083-[0-9]+-[0-9]+", tag):
        raise ValueError("Unexpected publication tag")
    credentials = base64.b64encode(("shinil35:" + os.environ["GHCR_TOKEN"]).encode()).decode()
    request = urllib.request.Request(
        "https://ghcr.io/token?service=ghcr.io&scope=repository:shinil35/hermes-agent:pull,push",
        headers={"Authorization": "Basic " + credentials})
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        "https://ghcr.io/v2/shinil35/hermes-agent/manifests/" + tag.rsplit(":", 1)[1], method="HEAD",
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"})
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return
        raise RuntimeError(f"GHCR tag lookup refused (HTTP {error.code})") from None
    raise ValueError("Publication tag already exists; refusing overwrite")


def main():
    command, *args = sys.argv[1:]
    if command == "metadata":
        validate_metadata(*(json.loads(Path(p).read_text(encoding="utf-8-sig")) for p in args))
    elif command == "unpack":
        print(json.dumps(unpack_verified(*(Path(p) for p in args))))
    elif command == "unused-tag":
        require_unused_tag(*args)
    else:
        raise ValueError("Unsupported promotion command")


if __name__ == "__main__":
    main()
