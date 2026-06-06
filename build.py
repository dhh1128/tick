#!/usr/bin/env python3
"""Build the single-file zipapp executable `tick` from src/.

Run: python3 build.py [OUTPUT]   ->  OUTPUT (default: dist/tick), chmod +x,
shebang /usr/bin/env python3. Because tick has zero runtime dependencies, the
zipapp is just our own modules. Keeping the artifact under dist/ (gitignored)
keeps the repo root clean.

The version (single source of truth: pyproject.toml) is baked into the bundled
`tick/__init__.py`, because a zipapp has no package metadata for
importlib.metadata to read at runtime — so `tick --version` would otherwise
report the dev placeholder.
"""

import pathlib
import shutil
import sys
import tempfile
import tomllib
import zipapp

REPO_ROOT = pathlib.Path(__file__).resolve().parent


def _include(path: pathlib.Path) -> bool:
    """Keep compiled-bytecode cruft out of the archive so a local build (which
    runs against a populated src/__pycache__) matches CI's clean checkout — the
    artifact stays lean and reproducible."""
    return "__pycache__" not in path.parts and path.suffix != ".pyc"


def _version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


def main() -> None:
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("dist/tick")
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tick-build-") as tmp:
        build_root = pathlib.Path(tmp)
        shutil.copytree(REPO_ROOT / "src", build_root, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # Bake the version literal in, replacing the importlib.metadata lookup.
        init = build_root / "tick" / "__init__.py"
        doc = init.read_text().split("\n", 1)[0]  # keep the module docstring line
        init.write_text(f'{doc}\n\n__version__ = "{_version()}"\n')
        zipapp.create_archive(
            build_root,
            target=str(out),
            interpreter="/usr/bin/env python3",
            main="tick.cli:main",
            filter=_include,
        )
    out.chmod(0o755)
    print(f"built {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
