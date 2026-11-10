"""Keep the public fork identity compatible with Python's package metadata."""

import re
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
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
    version = Version(distribution[0])
    stable_requirement = SpecifierSet(f"=={version.base_version}")
    assert version in stable_requirement
    if version.base_version == project["version"]:
        assert slim.specifier == stable_requirement
        assert version in slim.specifier


def test_current_overlay_reproduces_maintenance_api_source() -> None:
    """The stable build and reviewed maintenance source must have one truth."""
    overlay = ROOT / ".github/patches/api-1.2.1-fork.1.patch"
    # Reverse-check every hunk against current production bytes without writing files.
    # This also runs in a shallow source checkout with no release tags available.
    subprocess.run(["git", "apply", "--reverse", "--check", "--unidiff-zero", str(overlay)], cwd=ROOT, check=True)
    from hindsight_api import __version__

    assert __version__ == "1.2.1-fork.1"
    # Reuse remains opt-in across the component update.
    from hindsight_api.config import DEFAULT_RETAIN_STABLE_CHUNKS

    assert DEFAULT_RETAIN_STABLE_CHUNKS is False
