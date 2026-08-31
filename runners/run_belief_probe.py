"""CLI compatibility wrapper for :mod:`runners.belief_probe`."""

from .belief_probe import main


if __name__ == "__main__":
    raise SystemExit(main())

