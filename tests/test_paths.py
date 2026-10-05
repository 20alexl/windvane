"""Project resolution: which project a file belongs to."""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_the_engine_package_carries_no_project_marker():
    """A module named like a marker (the install check was ``setup.py`` once)
    makes the resolver take the package for a project of its own, and every
    edit inside it is filed under ``<repo>/windvane``."""
    from windvane.paths import _PROJECT_MARKERS

    present = sorted(m for m in _PROJECT_MARKERS if (REPO / "windvane" / m).exists())
    assert present == []


def test_a_file_inside_the_engine_package_resolves_to_the_repository():
    from windvane.paths import _normalize_path, resolve_project_for_file

    resolved = resolve_project_for_file(str(REPO / "windvane" / "tools.py"), str(REPO))
    assert resolved == _normalize_path(str(REPO))
