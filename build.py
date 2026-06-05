#!/usr/bin/env python3
"""Build the single-file zipapp executable `tick` from src/.

Run: python3 build.py [OUTPUT]   ->  OUTPUT (default: dist/tick), chmod +x,
shebang /usr/bin/env python3. Because tick has zero runtime dependencies, the
zipapp is just our own modules. Keeping the artifact under dist/ (gitignored)
keeps the repo root clean.
"""

import pathlib
import sys
import zipapp


def _include(path: pathlib.Path) -> bool:
    """Keep compiled-bytecode cruft out of the archive so a local build (which
    runs against a populated src/__pycache__) matches CI's clean checkout — the
    artifact stays lean and reproducible."""
    return "__pycache__" not in path.parts and path.suffix != ".pyc"


def main() -> None:
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("dist/tick")
    out.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(
        "src",
        target=str(out),
        interpreter="/usr/bin/env python3",
        main="tick.cli:main",
        filter=_include,
    )
    out.chmod(0o755)
    print(f"built {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
