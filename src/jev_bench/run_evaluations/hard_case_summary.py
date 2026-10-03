"""Render the runner final summary without invoking providers."""
from rich.table import Table
from jev_bench.storage.io import atomic_write
from jev_bench.suites.hard_case.metrics import results_frame, summarize
from jev_bench.run_evaluations.plots.hard_case import plot_results

def report(store, console):
    frame = results_frame(list(store.latest.values()))
    if frame.empty:
        return None
    summary = summarize(frame, results_frame(store.history))
    atomic_write(store.directory / "summary.csv", lambda handle: summary.to_csv(handle, index=False))
    plot_results(frame, summary, store.directory)
    table = Table(title="Hard-case insurance judgment repeatability")
    for name in ["Model", "Valid", "Latest failures", "Historical failures", "p50 ms", "p95 ms"]:
        table.add_column(name)
    for row in summary.itertuples():
        table.add_row(row.model, f"{row.successful_repetitions}/{row.attempted_repetitions}",
                      str(row.validation_failures), str(row.historical_failures),
                      f"{row.latency_ms_p50:.1f}", f"{row.latency_ms_p95:.1f}")
    console.print(table)
    console.print(f"Six independent probability distributions saved in summary.csv. "
                  f"Results: {store.directory.resolve()}", markup=False)
    return summary
