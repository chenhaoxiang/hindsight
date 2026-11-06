"""Keep the public fork identity compatible with Python's package metadata."""

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
OVERLAYS = sorted((ROOT / ".github" / "patches").glob("api-*-fork.*.patch"))


def test_fork_api_overlay_exists() -> None:
    assert OVERLAYS, "The stable API build must have an explicit fork overlay"


@pytest.mark.parametrize("overlay", OVERLAYS, ids=lambda path: path.name)
def test_fork_display_and_distribution_versions_agree(overlay: Path) -> None:
    identity = re.fullmatch(r"api-(\d+\.\d+\.\d+)-fork\.(\d+)\.patch", overlay.name)
    assert identity is not None
    base, revision = identity.groups()
    patch = overlay.read_text()
    distribution = re.findall(r'^\+version = "([^"]+)"$', patch, re.MULTILINE)
    display = re.findall(r'^\+__version__ = "([^"]+)"$', patch, re.MULTILINE)
    assert distribution == [f"{base}+fork.{revision}"]
    assert display == [f"{base}-fork.{revision}"]
    assert str(Version(distribution[0])) == distribution[0]


@pytest.mark.parametrize("overlay", OVERLAYS, ids=lambda path: path.name)
def test_fork_distribution_satisfies_unchanged_api_wrapper(overlay: Path) -> None:
    patch = overlay.read_text()
    distribution = re.findall(r'^\+version = "([^"]+)"$', patch, re.MULTILINE)
    assert len(distribution) == 1
    project = tomllib.loads((ROOT / "hindsight-api" / "pyproject.toml").read_text())["project"]
    dependencies = [Requirement(value) for value in project["dependencies"]]
    slim = next(dependency for dependency in dependencies if dependency.name == "hindsight-api-slim")
    # A Python post-release does not satisfy ==base; a local version does. Keep
    # the unchanged full API wrapper usable without publishing under its name.
    assert Version(distribution[0]) in slim.specifier
