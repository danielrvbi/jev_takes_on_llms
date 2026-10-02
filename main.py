import argparse
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from benchmark.cases import CASES
from benchmark.providers import MODELS
from benchmark.runner import run_benchmark


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
    parser = argparse.ArgumentParser(description="Structured decision repeatability benchmark")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help=f"use all {len(MODELS)} models")
    group.add_argument("--models", nargs="+", choices=MODELS, help="models to measure")
    parser.add_argument("--repetitions", type=positive_integer, default=30)
    parser.add_argument("--warmups", type=nonnegative_integer, default=2)
    parser.add_argument("--case", nargs="+", action="extend", type=int,
                        choices=range(1, 11), dest="case_ids", help="case IDs; flag may be repeated")
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    console = Console()
    models = list(MODELS) if args.all else list(dict.fromkeys(args.models))
    cases = [case for case in CASES if args.case_ids is None or case.case_id in args.case_ids]
    try:
        failures = run_benchmark(models, cases, args.repetitions, args.warmups,
                                 args.output_dir, console=console)
        return 1 if failures else 0
    except KeyboardInterrupt:
        console.print("Interrupted. Saved repetitions will be reused on resume.")
        return 130
    except Exception as exc:
        console.print(f"Benchmark error: {type(exc).__name__}: {exc}", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
