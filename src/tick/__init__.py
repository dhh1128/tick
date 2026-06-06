"""tick — an extremely lightweight, all-local task/knowledge ledger for one codebase."""


def _detect_version() -> str:
    """Resolve the version from a single source of truth.

    - pip/pipx install: read it from the package metadata.
    - source checkout (tests, `python -m tick`): fall back to pyproject.toml so
      dev still reports the real number instead of a placeholder.
    - zipapp: never reaches here — build.py bakes a literal `__version__` in,
      because a zipapp has no metadata for importlib.metadata to read.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("tick")
    except PackageNotFoundError:
        import re
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        try:
            m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
        except OSError:
            m = None
        return m.group(1) if m else "0.0.0+dev"


__version__ = _detect_version()
