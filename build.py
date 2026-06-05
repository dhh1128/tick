#!/usr/bin/env python3
"""Build the single-file zipapp executable `tick` from src/.

Run: python3 build.py  ->  ./tick  (chmod +x, shebang /usr/bin/env python3)
Because tick has zero runtime dependencies, the zipapp is just our own modules.
"""

import pathlib
import zipapp


def main() -> None:
    out = pathlib.Path("tick")
    zipapp.create_archive(
        "src",
        target=str(out),
        interpreter="/usr/bin/env python3",
        main="tick.cli:main",
    )
    out.chmod(0o755)
    print(f"built {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
