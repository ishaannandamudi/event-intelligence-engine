"""Explicit command entry point; importing EIE never starts networking."""

from eie.runtime.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
