"""Suite-specific figures from accepted probability rows and cold timings."""
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from jev_bench.suites.hard_case.schemas import PROBABILITY_FIELDS


def plot_results(frame, summary, directory):
    target = Path(directory) / 'plots'
    target.mkdir(exist_ok=True)
    means = summary.set_index('model')[[f'{field}_mean' for field in PROBABILITY_FIELDS]]
    figure, axes = plt.subplots(figsize=(11, max(3, len(means) * .7)))
    image = axes.imshow(means.to_numpy(), vmin=0, vmax=1, cmap='viridis', aspect='auto')
    axes.set_xticks(range(len(PROBABILITY_FIELDS)), [f.replace('_', '\n') for f in PROBABILITY_FIELDS])
    axes.set_yticks(range(len(means)), means.index)
    axes.set_title('Accepted independent insurance probabilities (means)')
    figure.colorbar(image, ax=axes)
    figure.tight_layout()
    figure.savefig(target / 'probabilities.png', dpi=150)
    plt.close(figure)
    figure, axes = plt.subplots(figsize=(9, max(3, len(summary) * .7)))
    axes.barh(summary.model, summary.latency_ms_p50)
    axes.set_xlabel('Cold wall time, ms (median; includes local startup and teardown)')
    figure.tight_layout()
    figure.savefig(target / 'latency.png', dpi=150)
    plt.close(figure)
