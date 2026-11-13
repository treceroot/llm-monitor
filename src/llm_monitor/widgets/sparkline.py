"""Sparkline widget: filled area chart rendered with Unicode block characters.

Colors are resolved from the active Textual theme at render time, so theme
switches apply immediately.

The chart renders at twice the vertical resolution of the text rows by using
the lower-half block (▄) in addition to the full block (█), so a 2-row chart
shows 4 distinct levels. The area is filled from the baseline up, each column
is tinted by a gradient that goes from a faint tint of the theme color (low
values) to the full color (high values), and the most recent sample is
highlighted so the "live" edge reads at a glance.
"""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from ..theme import color_ramp, theme_color

# full block, lower-half block, baseline tick, empty
_FULL = "█"
_LOWER = "▄"
_BASE = "─"


class Sparkline(Static):
    """Renders a list of floats as a filled area chart.

    Args:
        color_var: name of a theme variable (e.g. "accent", "primary").
        height: number of text lines used for the chart.
    """

    def __init__(self, color_var: str = "accent", height: int = 6, **kwargs):
        super().__init__("", **kwargs)
        self._color_var = color_var
        self._height = height
        self._data: list[float] = []

    def update_data(self, data: list[float], height: int | None = None) -> None:
        """Update the chart; `height` sets how many rows the sparkline uses."""
        self._data = data
        if height is not None and height != self._height:
            self._height = max(1, height)
            self.styles.height = self._height
        self.refresh()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def render(self) -> Text:
        """Build the chart from the current size (re-runs on resize)."""
        return self._build_text()

    def _build_text(self) -> Text:
        width = self.size.width
        height = self.size.height or self._height
        out = Text(no_wrap=True)
        if width < 1:
            return out  # not laid out yet

        data = self._data
        if not data:
            # idle state: a faint baseline with a centered hint
            hint = "waiting for data…"
            pad = max(0, (width - len(hint)) // 2)
            muted = theme_color(self.app, "text_muted")
            out.append(" " * pad, style="")
            out.append(hint, style=muted)
            out.append("─" * max(0, width - pad - len(hint)), style=muted)
            return out

        height = max(1, height)
        cols = data[-width:]
        vmax = max(max(cols), 1e-9)
        units = 2 * height  # vertical resolution (half-rows)
        bars = [max(0, min(units, int(round(v / vmax * units)))) for v in cols]

        ramp = color_ramp(self.app, self._color_var, steps=max(4, units))
        base_style = theme_color(self.app, "text_muted")
        live_style = f"{ramp[-1]} bold"

        for r in range(height - 1, -1, -1):  # top row first
            if r != height - 1:
                out.append("\n")
            run_char: str | None = None
            run_style: str | None = None
            run_len = 0
            for i, level in enumerate(bars):
                if level >= 2 + 2 * r:
                    ch = _FULL
                elif level == 1 + 2 * r:
                    ch = _LOWER
                elif r == 0:
                    ch = _BASE
                else:
                    ch = " "
                if ch in (_BASE, " "):
                    style = base_style
                elif i == len(bars) - 1:
                    style = live_style
                else:
                    style = ramp[min(level - 1, len(ramp) - 1)]
                if ch == run_char and style == run_style:
                    run_len += 1
                else:
                    if run_char is not None:
                        out.append(run_char * run_len, style=run_style)
                    run_char, run_style, run_len = ch, style, 1
            if run_char is not None:
                out.append(run_char * run_len, style=run_style)
        return out
