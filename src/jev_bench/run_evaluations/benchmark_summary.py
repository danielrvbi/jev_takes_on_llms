"""Render the runner final summary without invoking providers."""
from rich.table import Table
from jev_bench.storage.io import atomic_write
from jev_bench.suites.benchmark.metrics import results_frame, summarize
from jev_bench.run_evaluations.plots.benchmark import plot_results

def report(store, console):
    frame = results_frame(list(store.latest.values()))
    if frame.empty:
        return None
    summary = summarize(frame, results_frame(store.history))
    atomic_write(store.directory / "summary.csv", lambda handle: summary.to_csv(handle, index=False))
    plot_results(frame, summary, store.directory)
    table = Table(title="Structured decision repeatability / distribution")
    for column in ["Model", "Case", "Valid", "Web", "Safe", "Fresh", "Route", "p95 ms"]:
        table.add_column(column, justify="left" if column == "Model" else "right", no_wrap=True)
    def number(value, specification=".3f"):
        return "—" if value != value else format(value, specification)
    for row in summary.itertuples():
        table.add_row(row.model, str(row.case_id), f"{row.successful_repetitions}/{row.attempted_repetitions}",
                      number(row.requires_web_probability_mean), number(row.is_safe_probability_mean),
                      number(row.expected_freshness_mean), number(row.route_consistency, ".0%"),
                      number(row.latency_ms_p95, ".1f"))
    console.print(table)
    console.print("Web/Safe/Fresh: means; Route: consistency; Valid: valid/attempted.")
    console.print(f"Latest failures: {int(summary.validation_failures.sum())}; "
                  f"historical failures: {int(summary.historical_failures.sum())}. "
                  f"Results: {store.directory.resolve()}", markup=False)
    return summary
