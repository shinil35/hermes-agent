"""Validate image provenance acceptance without pretending to run a container."""
import pytest

from scripts.arm64.image_smoke import check_stamp


@pytest.fixture
def stamp():
    return {"schemaVersion": 2, "commit": "a" * 40, "baseVersion": "0.22.3",
            "distribution": "docker", "updateMechanism": "external", "source": "ci", "dirty": False}


def test_exact_downstream_stamp(stamp):
    check_stamp(stamp, "a" * 40)


@pytest.mark.parametrize("key,value", [
    ("commit", "b" * 40), ("baseVersion", "0.23.0"),
    ("updateMechanism", "self"), ("dirty", True), ("schemaVersion", 1),
])
def test_incompatible_stamp_rejected(stamp, key, value):
    stamp[key] = value
    with pytest.raises(AssertionError):
        check_stamp(stamp, "a" * 40)
