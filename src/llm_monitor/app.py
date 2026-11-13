"""Main application: layout, polling loop, keybindings."""
from __future__ import annotations

import asyncio
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Static

from .collectors.hardware import cpu_poll, gpu_poll, ram_poll
from .collectors.llama import LlamaCollector
from .collectors.types import (
    CpuSnapshot,
    GpuSnapshot,
    LlamaSnapshot,
    ModelProps,
    RamSnapshot,
)
from .layout import LayoutBudget, PREFERRED
from .theme import (
    SYSTEM_THEME_NAME,
    build_omarchy_theme,
    omarchy_signature,
    theme_color,
)
from .widgets.panels import (
    CpuPanel,
    FooterBar,
    GpuPanel,
    HeaderBar,
    MetricsPanel,
    RamPanel,
)
from .widgets.sparkline import Sparkline

HISTORY = 120  # samples kept per chart (~60s at the default 0.5s interval)


class _Body(VerticalScroll):
    """The scrollable dashboard body.

    A terminal resize resizes the body in the layout pass, but the box
    heights were computed for the previous size. Recompute the budget
    once the refresh settles, so the dashboard refits immediately
    instead of waiting for the next poll. (The app-level ``on_resize``
    is never delivered by Textual for terminal resizes — the event goes
    to the screen.)
    """

    def on_resize(self, event) -> None:
        self.app.call_after_refresh(self.app._update_layout)


def _set_height(widget, height: int) -> None:
    """Set a widget height only when it actually changes."""
    if widget.styles.height.value != height:
        widget.styles.height = height


class LLMMonitorApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "llm-monitor"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("t", "cycle_theme", "Theme"),
    ]

    def __init__(
        self,
        url: str = "http://127.0.0.1:8001",
        interval: float = 0.5,
        theme: Optional[str] = None,
    ):
        super().__init__()
        self.server_url = url
        self.poll_interval = interval
        if theme:
            self.theme = theme
        self._collector = LlamaCollector(url)
        self._hist_decode: list[float] = []
        self._hist_prefill: list[float] = []
        self._poll_task: Optional[asyncio.Task] = None
        self._model_alias = ""
        self._model_path = ""
        self._n_ctx: Optional[int] = None
        self._props: Optional[ModelProps] = None
        self._last_snap: Optional[LlamaSnapshot] = None
        self._last_gpu: Optional[GpuSnapshot] = None
        self._last_cpu: Optional[CpuSnapshot] = None
        self._last_ram: Optional[RamSnapshot] = None
        # omarchy theme sync (--theme pins a textual theme instead)
        self._theme_pinned = theme is not None
        self._omarchy_sig: Optional[tuple[str, float]] = None

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        # everything lives in a scrollable body: on short terminals nothing
        # is lost — the boxes shrink and scroll instead of clipping
        with _Body(id="body"):
            # 1) hardware: CPU + RAM on top, GPUs just below
            with Container(id="sys-section"):
                yield CpuPanel(id="cpu-panel")
                yield RamPanel(id="ram-panel")
            yield GpuPanel(id="gpu-panel")
            # 2) llm.cpp area:
            #   - compact decode / prefill charts (side by side)
            #   - one box with every metric: MODEL | SAMPLING | TOKENS | SLOTS |
            #     CACHE | SPECULATIVE | REQUESTS
            with Container(id="llm-section"):
                with Container(classes="chart-grid"):
                    with Container(classes="chart-card"):
                        yield Static("DECODE  —", id="decode-value", classes="chart-value")
                        yield Sparkline(color_var="accent", height=2, id="decode-chart")
                    with Container(classes="chart-card"):
                        yield Static("PREFILL  —", id="prefill-value", classes="chart-value")
                        yield Sparkline(color_var="primary", height=2, id="prefill-chart")
                with Container(classes="combined-card"):
                    yield MetricsPanel(id="metrics-panel")
        yield FooterBar(id="footer")

    async def on_mount(self) -> None:
        self._sync_omarchy_theme()
        self.set_interval(self.poll_interval, self._on_tick)
        self._on_tick()
        self.run_worker(self._fetch_props, exclusive=False, group=False)

    def on_resize(self) -> None:
        """Initial size known (mount): compute the layout budget.

        Later terminal resizes are handled by ``_Body.on_resize``.
        """
        self._update_layout()

    # ------------------------------------------------------------------
    # adaptive layout
    # ------------------------------------------------------------------

    def _update_layout(self) -> None:
        """Fit the whole dashboard into the terminal, whatever its size.

        The dashboard is measured top-down. RAM, GPU, DECODE and PREFILL
        have priority and are never cut; the metrics box is compacted by
        metric priority, the CPU core grid uses the rows the RAM box leaves
        free and the GPU box absorbs whatever is left over.
        """
        try:
            body = self.query_one("#body", VerticalScroll)
            sys_section = self.query_one("#sys-section", Container)
            gpu = self.query_one("#gpu-panel", GpuPanel)
            llm_section = self.query_one("#llm-section", Container)
            chart_grid = self.query_one(".chart-grid", Container)
            combined = self.query_one(".combined-card", Container)
            cpu = self.query_one("#cpu-panel", CpuPanel)
            ram = self.query_one("#ram-panel", RamPanel)
            metrics = self.query_one("#metrics-panel", MetricsPanel)
        except Exception:
            return
        available = body.size.height
        if not available:
            return  # not laid out yet

        n_gpus = max(1, len(self._last_gpu.gpus) if self._last_gpu else 1)

        # --- how many rows may the metrics box use? ---------------------
        # the mandatory blocks come first: RAM, GPU, DECODE, PREFILL
        procs, spark_rows = PREFERRED.procs, PREFERRED.spark_rows
        metrics_budget = 3
        for procs in (PREFERRED.procs, 4, 3, 2, 1):
            ram_lines = 1 + procs
            gpu_min = 2 + 2 * n_gpus + 1 + procs
            # the core grid may use the rows the RAM box leaves free
            cpu_rows = max(1, min(PREFERRED.cpu_rows, ram_lines - 1))
            sys_h = 2 + max(1 + cpu_rows, ram_lines)
            charts_h = 3 + spark_rows
            # 4 rows of margin + 2 rows of border around the metrics box
            metrics_budget = available - 4 - sys_h - gpu_min - charts_h - 2
            if metrics_budget >= 3:
                break
            if spark_rows > 1:
                # the sparklines shrink before the process lists
                spark_rows = 1
                charts_h = 4
                metrics_budget = available - 4 - sys_h - gpu_min - charts_h - 2
                if metrics_budget >= 3:
                    break

        budget = LayoutBudget(
            cpu_rows=cpu_rows,
            procs=procs,
            spark_rows=spark_rows,
            metrics_rows=max(3, metrics_budget),
        )

        # --- render every panel with the budget (a single pass) --------
        cpu.update_cpu(self._last_cpu, budget.cpu_rows)
        ram.update_ram(self._last_ram, budget.procs)
        gpu.update_gpus(self._last_gpu, budget.procs)
        metrics.update_metrics(self._last_snap, self._props, budget.metrics_rows)
        self.query_one("#decode-chart", Sparkline).update_data(
            self._hist_decode, budget.spark_rows
        )
        self.query_one("#prefill-chart", Sparkline).update_data(
            self._hist_prefill, budget.spark_rows
        )

        # --- place the boxes with the height they really need ----------
        sys_h = 2 + max(cpu.content_lines, ram.content_lines)
        charts_h = 3 + budget.spark_rows
        metrics_h = 2 + metrics.content_lines
        llm_h = charts_h + 1 + metrics_h
        # 3 rows of margin: before the hardware row, the GPU box and the
        # llama.cpp section (the gap inside the llama.cpp section is already
        # part of llm_h)
        gpu_h = available - 3 - sys_h - llm_h
        gpu_min = 2 + 2 * n_gpus + 1 + budget.procs
        while gpu_h < gpu_min and budget.procs > 1:
            # the core grid wrapped over more lines than expected: give the
            # space back to the GPU box by showing fewer processes
            budget.procs -= 1
            ram.update_ram(self._last_ram, budget.procs)
            gpu.update_gpus(self._last_gpu, budget.procs)
            sys_h = 2 + max(cpu.content_lines, ram.content_lines)
            gpu_h = available - 3 - sys_h - llm_h
            gpu_min = 2 + 2 * n_gpus + 1 + budget.procs

        _set_height(sys_section, sys_h)
        _set_height(chart_grid, charts_h)
        _set_height(combined, metrics_h)
        _set_height(metrics, metrics.content_lines)
        _set_height(llm_section, llm_h)
        _set_height(gpu, max(gpu_h, gpu_min))

    def _sync_omarchy_theme(self) -> None:
        """Adopt (and follow) the machine's active Omarchy theme.

        It is registered as the stable ``system`` theme, so the theme cycle
        always offers "whatever this machine is using" as a single entry.
        Two cheap stat() calls per tick; the theme is only rebuilt when the
        name or its colors.toml mtime changes.
        """
        if self._theme_pinned:
            return
        sig = omarchy_signature()
        if sig is None or sig == self._omarchy_sig:
            return
        theme = build_omarchy_theme(sig[0])
        if theme is None:
            return
        self._omarchy_sig = sig
        self.register_theme(theme)
        self._activate_theme(SYSTEM_THEME_NAME)

    def _activate_theme(self, name: str) -> None:
        """Activate a theme, re-applying it when its name was re-registered.

        Assigning ``self.theme`` the same value is a no-op in Textual, so a
        rebuilt ``system`` theme needs its watcher called explicitly.
        """
        if self.theme == name:
            watch = getattr(self, "_watch_theme", None)
            if watch is not None:
                watch(name)
        else:
            self.theme = name

    async def on_unmount(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
        await self._collector.close()

    # ------------------------------------------------------------------
    # polling
    # ------------------------------------------------------------------

    def _on_tick(self) -> None:
        self._sync_omarchy_theme()
        if self._poll_task is not None and not self._poll_task.done():
            return  # previous poll still running (e.g. slow network)
        self._poll_task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        cpu_snap = cpu_poll()
        ram_snap = ram_poll()
        try:
            snap, gpu_snap = await asyncio.gather(
                self._collector.poll(), asyncio.to_thread(gpu_poll)
            )
        except Exception:
            snap = LlamaSnapshot(reachable=False, error="poll failed")
            gpu_snap = GpuSnapshot(ok=False, error="poll failed")
        self._apply(snap, gpu_snap, cpu_snap, ram_snap)

    async def _fetch_props(self) -> None:
        props = await self._collector.fetch_props()
        if props.reachable:
            self._props = props.props
        if props.alias:
            self._model_alias = props.alias
        if props.model:
            self._model_path = props.model
        if props.n_ctx:
            self._n_ctx = props.n_ctx
        if self._n_ctx is None:
            try:
                client = await self._collector._get_client()
                resp = await client.get("/slots")
                resp.raise_for_status()
                slots = resp.json()
                slots = slots if isinstance(slots, list) else slots.get("slots", [])
                if slots and slots[0].get("n_ctx"):
                    self._n_ctx = int(slots[0]["n_ctx"])
            except Exception:
                pass
        if self._header is not None:
            self._refresh_header()
        try:
            self.query_one("#metrics-panel", MetricsPanel).update_metrics(
                self._last_snap, self._props
            )
        except Exception:
            pass  # not mounted yet

    # ------------------------------------------------------------------
    # applying snapshots to widgets
    # ------------------------------------------------------------------

    @property
    def _header(self) -> Optional[HeaderBar]:
        try:
            return self.query_one(HeaderBar)
        except Exception:
            return None

    def _refresh_header(self) -> None:
        """Re-render the header merging model info into the latest snapshot."""
        header = self._header
        snap = self._last_snap
        if header is None or snap is None:
            return
        snap.alias = self._model_alias or snap.alias
        snap.model = self._model_path or snap.model
        snap.n_ctx = self._n_ctx or snap.n_ctx
        if self._props is not None:
            snap.props = self._props
        header.update_header(snap, self.server_url)

    def _fmt_tps(
        self,
        label: str,
        instant: Optional[float],
        avg: Optional[float],
        hist: list[float],
        color_var: str,
    ) -> Text:
        """Chart line: label + big current t/s + min/avg/max from the history.

        The current value is bold and tinted with the same theme color as its
        sparkline, so the number and the chart below it read as one thing.
        """
        out = Text(no_wrap=True)
        muted = theme_color(self, "text_muted")
        out.append(label, style=muted)
        out.append("  ")
        if instant is not None:
            out.append(f"{instant:.1f}", style=f"{theme_color(self, color_var)} bold")
            out.append(" t/s", style=muted)
            stats: list[str] = []
            if hist:
                stats.append(f"min {min(hist):.1f}")
            if avg is not None:
                stats.append(f"avg {avg:.1f}")
            if hist:
                stats.append(f"max {max(hist):.1f}")
            if stats:
                out.append("   ", style="")
                out.append(" · ".join(stats), style=muted)
        elif avg is not None:
            out.append(f"{avg:.1f} t/s (avg)", style=muted)
        else:
            out.append("—", style=muted)
        return out

    def _apply(
        self,
        snap: LlamaSnapshot,
        gpu_snap: GpuSnapshot,
        cpu_snap: CpuSnapshot,
        ram_snap: RamSnapshot,
    ) -> None:
        # header
        self._last_snap = snap
        self._last_gpu = gpu_snap
        self._last_cpu = cpu_snap
        self._last_ram = ram_snap
        self._refresh_header()

        # charts
        if snap.decode_tps is not None:
            self._hist_decode = (self._hist_decode + [snap.decode_tps])[-HISTORY:]
        if snap.prefill_tps is not None:
            self._hist_prefill = (self._hist_prefill + [snap.prefill_tps])[-HISTORY:]
        try:
            self.query_one("#decode-value", Static).update(
                self._fmt_tps(
                    "DECODE", snap.decode_tps, snap.decode_avg_tps, self._hist_decode, "accent"
                )
            )
            self.query_one("#prefill-value", Static).update(
                self._fmt_tps(
                    "PREFILL", snap.prefill_tps, snap.prefill_avg_tps, self._hist_prefill, "primary"
                )
            )
        except Exception:
            return  # widgets not mounted yet

        # panels + adaptive layout (this also refreshes the sparklines)
        self._update_layout()

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._poll_task = None
        self._on_tick()

    def action_cycle_theme(self) -> None:
        # the machine theme is registered as "system"; keep it first and hide
        # any legacy "omarchy-*" alias so each theme appears only once
        themes = [
            name for name in self.available_themes if not name.startswith("omarchy-")
        ]
        if SYSTEM_THEME_NAME in themes:
            themes.remove(SYSTEM_THEME_NAME)
            themes.insert(0, SYSTEM_THEME_NAME)
        if not themes:
            return
        index = themes.index(self.theme) if self.theme in themes else -1
        self._activate_theme(themes[(index + 1) % len(themes)])
