import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def plot_results(frame, summary, output_dir):
    directory = Path(output_dir) / "plots"
    directory.mkdir(parents=True, exist_ok=True)
    models = list(frame.model.unique())
    cases = sorted(frame.case_id.unique())
    if not cases:
        return
    labels = [model.replace("mistral-", "mistral-\n") for model in models]
    specifications = [
        ("requires_web_probability", "requires_web", "Requires web probability", (0, 1)),
        ("is_safe_probability", "is_safe", "Safe to assist probability", (0, 1)),
        ("expected_freshness", "freshness", "Expected freshness (0–5)", (0, 5)),
        ("latency_ms", "latency", "Measured latency (ms; includes failed calls)", None),
    ]
    columns = min(2, len(cases))
    rows = math.ceil(len(cases) / columns)
    for field, filename, title, limits in specifications:
        fig, axes = plt.subplots(rows, columns, figsize=(max(7, len(models) * 2.4), rows * 3.5),
                                 squeeze=False, constrained_layout=True)
        for case_id, ax in zip(cases, axes.flat):
            panel = frame[frame.case_id == case_id]
            if field != "latency_ms":
                panel = panel[panel.validation_success]
            panel = panel.dropna(subset=[field])
            for index, model in enumerate(models):
                values = panel.loc[panel.model == model, field].to_numpy()
                if len(values):
                    ax.boxplot([values], positions=[index], orientation="horizontal", widths=0.5,
                               manage_ticks=False, showfliers=False)
                    # Deterministic vertical offsets; no sampling seed is needed.
                    offsets = np.linspace(-0.12, 0.12, len(values)) if len(values) > 1 else [0]
                    ax.scatter(values, index + np.asarray(offsets), s=14, alpha=0.6)
                else:
                    ax.text(0.5, index, "No data", ha="center", va="center",
                            rotation=90, transform=ax.get_yaxis_transform(), color="gray")
            ax.set_title(f"Case {int(case_id)}")
            ax.set_yticks(range(len(models)), labels, fontsize=8)
            ax.set_ylim(len(models) - 0.5, -0.5)
            if limits:
                ax.set_xlim(limits[0] - 0.03, limits[1] + 0.03)
            ax.grid(axis="x", alpha=0.2)
        for ax in list(axes.flat)[len(cases):]:
            ax.set_visible(False)
        fig.suptitle(title)
        fig.savefig(directory / f"{filename}.png", dpi=150)
        plt.close(fig)

    matrix = summary.pivot(index="model", columns="case_id", values="route_consistency").reindex(
        index=models, columns=cases)
    fig, ax = plt.subplots(figsize=(max(6, len(cases) * 0.8), max(2.5, len(models) * 0.65)),
                           constrained_layout=True)
    colormap = plt.get_cmap("viridis").with_extremes(bad="#eeeeee")
    plotted = ax.imshow(matrix.to_numpy(dtype=float), vmin=0, vmax=1, aspect="auto", cmap=colormap)
    ax.set_xticks(range(len(cases)), [f"Case {int(case_id)}" for case_id in cases])
    ax.set_yticks(range(len(models)), models)
    for row in range(len(models)):
        for column in range(len(cases)):
            value = matrix.iloc[row, column]
            ax.text(column, row, "No data" if np.isnan(value) else f"{value:.0%}",
                    ha="center", va="center", color="black" if np.isnan(value) or value > 0.55 else "white")
    ax.set_title("Route consistency — modal route / valid repetitions")
    fig.colorbar(plotted, ax=ax, label="Consistency")
    fig.savefig(directory / "route_consistency.png", dpi=150)
    plt.close(fig)
