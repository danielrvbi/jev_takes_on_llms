"""Inference commands. Help never constructs a provider."""
import argparse
from importlib import import_module
import sys

COMMANDS = {"benchmark": "benchmark_cli", "hard-case": "hard_case_cli",
            "jev-api": "jev_api", "prefix-pilot": "prefix_pilot"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    if not argv or argv[0] not in COMMANDS:
        parser.parse_args(argv)
    return import_module(f"jev_bench.run.{COMMANDS[argv[0]]}").main(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
