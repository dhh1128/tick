"""Entry point so `python -m tick` and the zipapp both work."""

from tick.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
