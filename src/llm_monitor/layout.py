"""Adaptive layout budget.

The dashboard has to fit in any terminal, whatever its size. Instead of
hard-coding heights, the app computes a *budget*: how many rows each
panel may use. Panels render as much detail as their budget allows and
drop the least important rows first.

Priority (highest first), always honoured as long as there is room:

  1. RAM box          (heading + the heaviest processes)
  2. GPU box          (every device + the heaviest processes)
  3. DECODE chart     (value + sparkline)
  4. PREFILL chart    (value + sparkline)
  5. llama.cpp metrics box  (compacted by metric priority)
  6. CPU core grid    (coarse to fine)

Only when even that does not fit are processes dropped, as a last resort.
"""
from __future__ import annotations

from dataclasses import dataclass

# one row of margin before each section (hardware row, GPU box, llama.cpp
# section) plus the one between the charts and the metrics box
_SECTION_GAPS = 4


@dataclass
class LayoutBudget:
    """How many rows each panel may spend."""

    cpu_rows: int = 6       # rows of the per-core usage grid
    procs: int = 5          # processes listed (RAM and VRAM)
    spark_rows: int = 2     # rows of the t/s sparklines
    metrics_rows: int = 13  # content rows of the metrics box

    def as_dict(self) -> dict[str, int]:
        return {
            "cpu_rows": self.cpu_rows,
            "procs": self.procs,
            "spark_rows": self.spark_rows,
            "metrics_rows": self.metrics_rows,
        }


# what a roomy terminal gets
PREFERRED = LayoutBudget(
    cpu_rows=6,
    procs=5,
    spark_rows=2,
    metrics_rows=13,
)

# the smallest still useful layout: RAM, GPU, DECODE and PREFILL stay whole
MINIMUM = LayoutBudget(
    cpu_rows=1,
    procs=5,
    spark_rows=1,
    metrics_rows=3,
)

# order in which the budget is handed out: the flexible panels grow first
_GROW_ORDER = ("metrics_rows", "cpu_rows", "spark_rows")

# last resort: how many processes to drop, one step at a time
_PROCS_FALLBACK = (4, 3, 2, 1)


def total_height(budget: LayoutBudget, n_gpus: int) -> int:
    """Rows the whole dashboard needs with the given budget.

    The model mirrors the real layout:
      hardware row  2 borders + max(1 heading + cpu rows, 1 heading + procs)
      GPU box       2 borders + 2 lines per GPU + 1 table header + procs
      charts       2 borders + 1 value row + sparkline rows
      metrics box  2 borders + content rows
    plus one row of margin before each of the three sections.
    """
    hardware = 3 + max(budget.cpu_rows, budget.procs)
    gpu = 3 + 2 * n_gpus + budget.procs
    charts = 3 + budget.spark_rows
    metrics = 2 + budget.metrics_rows
    return _SECTION_GAPS + hardware + gpu + charts + metrics


def compute_budget(
    available: int, n_gpus: int = 1, metrics_rows: int = MINIMUM.metrics_rows
) -> LayoutBudget:
    """Distribute the available rows between the panels.

    The four blocks that must always be visible (RAM, GPU, DECODE,
    PREFILL) are reserved first; the remaining space is shared between the
    metrics box, the CPU core grid and the sparklines. `metrics_rows` is
    the height the metrics box really needs.
    """
    budget = LayoutBudget(
        cpu_rows=MINIMUM.cpu_rows,
        procs=MINIMUM.procs,
        spark_rows=MINIMUM.spark_rows,
        metrics_rows=metrics_rows,
    )
    for attr in _GROW_ORDER:
        cap = getattr(PREFERRED, attr)
        if attr == "metrics_rows":
            cap = min(cap, metrics_rows)
        while (
            getattr(budget, attr) < cap
            and total_height(budget, n_gpus) + 1 <= available
        ):
            setattr(budget, attr, getattr(budget, attr) + 1)
    return budget


def spare_rows(available: int, budget: LayoutBudget, n_gpus: int) -> int:
    """Rows left over after the panels have taken what they need."""
    return max(0, available - total_height(budget, n_gpus))
