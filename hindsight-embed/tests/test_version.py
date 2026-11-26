"""The embed package identity must match its distribution metadata."""

from pathlib import Path
import tomllib

from hindsight_embed import __version__


def test_version_matches_pyproject() -> None:
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]
    assert __version__ == project["version"]
