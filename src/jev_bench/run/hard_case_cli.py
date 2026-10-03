from jev_bench.storage.paths import PROJECT_ROOT, new_experiment
import argparse
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from jev_bench.providers.suites.hard_case import MODELS
from jev_bench.run.hard_case import run_hard_case_benchmark


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
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--input-profile", choices=("compact",), default="compact",
                        help="optional compatibility flag; this suite always uses compact input")
    parser.add_argument("--systemone-context", type=positive_integer, choices=(262144,), default=None,
                        help="use verified stock Tev aliases with this context")
    parser.add_argument("--prefix-experiment", action="store_true", help="separate Mistral unique-prefix experiment")
    parser.add_argument("--max-new-calls", type=positive_integer, default=None, help="cap new calls, including warm-ups")
    args = parser.parse_args(argv)
    if args.output_dir is None:
        args.output_dir = new_experiment("hard-case")
    return args


def main(argv=None):
    args = parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    console = Console()
    import shlex, sys
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--output-dir" not in arguments:
        command = "benchmark" if "benchmark" in __name__ else "hard-case"
        console.print("Resume: " + shlex.join(["python", "-m", "jev_bench.run", command, *arguments, "--output-dir", str(args.output_dir)]), markup=False, soft_wrap=True)

    models = list(MODELS) if args.all else list(dict.fromkeys(args.models))
    try:
        failures = run_hard_case_benchmark(models, args.repetitions, args.warmups,
                                           args.output_dir, console=console,
                                           input_profile=args.input_profile,
                                           systemone_context=args.systemone_context, prefix_experiment=args.prefix_experiment,
                                           max_new_calls=args.max_new_calls)
        return 1 if failures else 0
    except KeyboardInterrupt:
        console.print("Interrupted. Saved successful repetitions will be reused on resume.")
        return 130
    except Exception as exc:
        console.print(f"Hard-case benchmark error: {type(exc).__name__}: {exc}", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
