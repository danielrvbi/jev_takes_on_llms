"""Offline tools for saved experiments; never invoke a model."""
import argparse
import json
from pathlib import Path
import sys

from jev_bench.storage.paths import RESULTS_ROOT


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "report", "compare", "reconcile"):
        sub = commands.add_parser(command)
        if command in {"compare", "reconcile"}:
            sub.add_argument("--input-dirs", nargs="+", type=Path, required=True)
        else:
            sub.add_argument("--input-dir", type=Path, required=True)
        sub.add_argument("--output-dir", type=Path)
        if command in {"report", "compare"}:
            sub.add_argument("--suite", choices=("all", "benchmark", "hard_case"), default="all")
            sub.add_argument("--models", nargs="+")
            sub.add_argument("--case", nargs="+", action="extend", type=int, dest="case_ids")
            sub.add_argument("--include-raw", action="store_true")
    args = parser.parse_args(argv)
    sources = [p.resolve() for p in (args.input_dirs if hasattr(args, "input_dirs") else [args.input_dir])]
    output = (args.output_dir or RESULTS_ROOT / "reports" / (sources[0].name if len(sources) == 1 else "comparison")).resolve()
    try:
        if any(output.is_relative_to(source) or source.is_relative_to(output) for source in sources):
            raise ValueError("Evaluation output must be separate from source datasets")
        if any(any(part in {"quarantine", ".migration_backup"} or "contaminated" in part.lower()
                   for part in source.parts) for source in sources):
            raise ValueError("Quarantined results cannot be imported")
        if args.command == "validate":
            from jev_bench.run_evaluations.validation import validate_saved
            manifest = validate_saved(sources[0], output)
            print(json.dumps(manifest, indent=2))
            return 0 if manifest["complete"] else 1
        if args.command == "reconcile":
            from jev_bench.run_evaluations.reconcile import main as reconcile
            if len(sources) != 2:
                raise ValueError("Reconcile requires two source suite directories")
            return reconcile(["--suite-dir", str(sources[0]), "--mistral-dir", str(sources[1]), "--output-dir", str(output)])
        if args.command == "compare":
            from jev_bench.run_evaluations.comparison import build_combined_report
            report, _ = build_combined_report(sources, args.suite, args.models, args.case_ids,
                                             args.include_raw, output_directory=output)
            output.mkdir(parents=True, exist_ok=True)
            (output / "evaluation.md").write_text(report, encoding="utf-8")
        else:
            from jev_bench.run_evaluations.artifacts import render
            render(sources[0], output, suite=args.suite, models=args.models,
                   case_ids=args.case_ids, include_raw=args.include_raw)
        print(f"Saved evaluation to {output}")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Evaluation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
