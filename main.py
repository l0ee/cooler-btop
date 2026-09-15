"""Compatibility launcher for running Cooler btop from a source checkout."""

from cooler_btop.main import run_cli


if __name__ == "__main__":
    raise SystemExit(run_cli())
