"""Module entry point so `python -m antipatterns` works.

Delegates to `cli.main` and exits with its returned code.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
