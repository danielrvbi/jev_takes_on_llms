import argparse
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from hard_case.loader import HARD_CASE_DIR
from hard_case.providers import MODELS
from hard_case.runner import DEFAULT_OUTPUT_DIR, run_hard_case_benchmark


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def nonnegative_integer(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Independent hard-case insurance probability benchmark")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help=f"use all {len(MODELS)} models")
    group.add_argument("--models", nargs="+", choices=MODELS, help="models to measure")
    parser.add_argument("--repetitions", type=positive_integer, default=30)
    parser.add_argument("--warmups", type=nonnegative_integer, default=2)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--input-profile", choices=("compact",), default="compact",
                        help="optional compatibility flag; this suite always uses compact input")
    parser.add_argument("--systemone-context", type=positive_integer, choices=(262144,), default=None,
                        help="use verified stock Tev aliases with this context")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    load_dotenv(HARD_CASE_DIR.parent / ".env", override=False)
    console = Console()
    models = list(MODELS) if args.all else list(dict.fromkeys(args.models))
    try:
        failures = run_hard_case_benchmark(models, args.repetitions, args.warmups,
                                           args.output_dir, console=console,
                                           input_profile=args.input_profile,
                                           systemone_context=args.systemone_context)
        return 1 if failures else 0
    except KeyboardInterrupt:
        console.print("Interrupted. Saved successful repetitions will be reused on resume.")
        return 130
    except Exception as exc:
        console.print(f"Hard-case benchmark error: {type(exc).__name__}: {exc}", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
