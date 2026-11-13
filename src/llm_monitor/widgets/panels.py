"""Static panels: header, GPU, CPU, RAM and LLM stats.

All colors are resolved from the active theme at update time, so theme
switches apply immediately.
"""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import Static

from ..collectors.types import (
    CpuSnapshot,
    GpuSnapshot,
    LlamaSnapshot,
    ModelProps,
    ProcMem,
    RamSnapshot,
)
from ..theme import color_ramp_from, theme_color

BAR_WIDTH = 20
CORE_BAR_HEIGHT = 6
TOP_PROCS = 5  # only the N heaviest processes are listed (RAM and VRAM)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _theme(widget: Static, var: str) -> str:
    return theme_color(widget.app, var)


def _fmt_bytes(b: int) -> str:
    if b <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}" if unit != "B" else f"{int(b)} B"
        b /= 1024
    return f"{b:.1f} PB"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _load_color(widget: Static, pct: float) -> str:
    if pct >= 85:
        return _theme(widget, "error")
    if pct >= 60:
        return _theme(widget, "warning")
    return _theme(widget, "success")


def _temp_color(widget: Static, temp_c: float) -> str:
    """Hot GPUs stand out: orange from 80 °C, red from 90 °C."""
    if temp_c >= 90:
        return _theme(widget, "error")
    if temp_c >= 80:
        return _theme(widget, "warning")
    return _theme(widget, "text_muted")


def _bar(widget: Static, pct: float, width: int = BAR_WIDTH, color: str | None = None) -> Text:
    """A progress bar with a left-to-right intensity gradient.

    The filled portion ramps from a faint tint of the color up to the full
    color at the fill edge; the remaining track is a barely-there tint so the
    whole bar reads as one object.
    """
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    base = color or _load_color(widget, pct)
    out = Text(no_wrap=True)
    if filled:
        for c in color_ramp_from(widget.app, base, filled):
            out.append("█", style=c)
    if filled < width:
        out.append("░" * (width - filled), style=color_ramp_from(widget.app, base, 8)[0])
    return out


def _proc_rows(
    widget: Static,
    entries: list[tuple[str, ProcMem]],
    width: int,
    height: int | None = None,
    name_w: int = 14,
    header: bool = False,
) -> Text:
    """Process rows that span the whole box: name + tag on the left, size
    anchored to the right edge.

    Each entry is (tag, proc). The open space between the tag and the size
    is what fills the row, so the list always reaches the right border of
    the box. When `height` is given and the box has more rows than entries,
    the spare rows are distributed between the entries so the list also
    fills the vertical space.
    """
    muted = _theme(widget, "text_muted")
    out = Text(no_wrap=True)
    if not entries:
        out.append("  (no per-process data)", style=muted)
        return out
    tag_w = max(len(tag) for tag, _ in entries)
    sizes = [_fmt_bytes(p.mem_bytes) for _, p in entries]
    size_w = max(len(s) for s in sizes)
    # row = indent + name + gap + [tag + gap] + filler + gap + size
    tag_part = 2 + tag_w + 2 if tag_w else 0
    fill = max(2, width - (2 + name_w + 2 + tag_part + 2 + size_w))

    def row(name: str, tag: str, size: str, name_style: str, size_style: str) -> None:
        out.append("  ", style="")
        out.append(f"{name:<{name_w}}", style=name_style)
        out.append("  ", style="")
        if tag_w:
            out.append(f"{tag:<{tag_w}}", style=muted)
            out.append("  ", style="")
        out.append(" " * fill, style="")
        out.append(f"{size:>{size_w}}", style=size_style)

    if header:
        row("process", "gpu", "size", _theme(widget, "text_highlight"), muted)
        out.append("\n")

    # spare rows (box taller than the list): one gap after every entry,
    # the remainder settles at the bottom
    n = len(entries)
    spare = max(0, (height or 0) - (1 if header else 0) - n)
    base, extra = divmod(spare, n)
    gaps = [base + (1 if i >= n - extra else 0) for i in range(n)]

    for i, ((tag, p), size) in enumerate(zip(entries, sizes)):
        if i:
            # always at least one row separator, plus the spare-row gap
            out.append("\n" * (1 + gaps[i - 1]))
        name = p.name if len(p.name) <= name_w else p.name[: name_w - 1] + "…"
        row(name, tag, size, _theme(widget, "text"), _theme(widget, "text"))
    out.append("\n" * gaps[-1])
    return out


# ---------------------------------------------------------------------------
# header
# ---------------------------------------------------------------------------


class HeaderBar(Static):
    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._snap: LlamaSnapshot | None = None
        self._url = ""

    def update_header(self, snap: LlamaSnapshot, url: str) -> None:
        self._snap = snap
        self._url = url
        self.update(self._build_text())

    def _build_text(self) -> Text:
        snap = self._snap
        muted = _theme(self, "text_muted")
        out = Text(no_wrap=True)
        if snap is None:
            out.append("llm-monitor", style=_theme(self, "accent"))
            out.append("  ·  connecting…", style=muted)
            return out
        if not snap.reachable:
            out.append("● ", style=_theme(self, "error"))
            out.append("server offline", style=_theme(self, "error"))
            self._right_pad(out, self._url, muted)
            return out
        out.append("● ", style=_theme(self, "success"))
        name = snap.alias or snap.model or "llama.cpp"
        out.append(name, style=_theme(self, "text_highlight"))
        if snap.props and snap.props.quant:
            out.append(f"  ·  {snap.props.quant}", style=muted)
        if snap.n_ctx:
            out.append(f"  ·  ctx {snap.n_ctx // 1000}k", style=muted)
        if snap.spec and snap.spec.per_pos:
            out.append(f"  ·  spec({len(snap.spec.per_pos)})", style=muted)
        if snap.uptime_seconds is not None:
            out.append(f"  ·  up {_fmt_uptime(snap.uptime_seconds)}", style=muted)
        self._right_pad(out, self._url, muted)
        return out

    def _right_pad(self, out: Text, text: str, style: str) -> None:
        """Append `text` anchored to the right edge of the header.

        Falls back to a plain gap when there is not enough room (narrow
        terminal), so the left side never overlaps the url.
        """
        width = self.size.width
        used = len(out.plain)
        if width and width - used > len(text) + 2:
            out.append(" " * (width - used - len(text)), style="")
        else:
            out.append("  ", style="")
        out.append(text, style=style)


class FooterBar(Static):
    """Key hints, docked at the bottom of the screen."""

    HINTS = (
        ("q", "quit"),
        ("r", "refresh"),
        ("t", "theme"),
    )

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self.update(self._build_text())

    def _build_text(self) -> Text:
        muted = _theme(self, "text_muted")
        accent = _theme(self, "accent")
        out = Text(no_wrap=True)
        for i, (key, label) in enumerate(self.HINTS):
            if i:
                out.append("  ·  ", style=muted)
            out.append(key, style=f"{accent} bold")
            out.append(f" {label}", style=muted)
        return out


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------


class GpuPanel(Container):
    """All GPUs in one box: per-GPU metric blocks plus a unified, scrollable
    list of the processes using VRAM (tagged with the GPU they run on)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._snap: GpuSnapshot | None = None
        self._head: Static | None = None
        self._procs: Static | None = None
        self._procs_limit = TOP_PROCS

    def compose(self) -> ComposeResult:
        yield Static("", classes="panel-head")
        yield Static("", classes="proc-list")

    def on_mount(self) -> None:
        self._head = self.query_one(".panel-head", Static)
        self._procs = self.query_one(".proc-list", Static)
        self._refresh()

    def on_resize(self, event) -> None:
        # The layout pass resizes the panel (terminal resize, budget
        # change) before the next poll; rebuild the rows right away so
        # they are never rendered for the previous size.
        self._refresh()

    def update_gpus(self, snap: GpuSnapshot, procs: int = TOP_PROCS) -> None:
        self._snap = snap
        self._procs_limit = max(0, procs)
        self._refresh()

    def _refresh(self) -> None:
        if self._head is None or self._procs is None:
            return
        snap = self._snap
        if snap is None:
            self._head.update(Text("GPUs  (waiting…)", style=_theme(self, "text_muted")))
            self._procs.update(Text("", style=_theme(self, "text_muted")))
            return
        if not snap.ok:
            self._head.update(Text(f"GPUs  {snap.error}", style=_theme(self, "error")))
            self._procs.update(Text("", style=_theme(self, "text_muted")))
            return
        self._head.update(self._build_head(snap))
        self._procs.update(self._build_procs(snap))

    def _build_head(self, snap: GpuSnapshot) -> Text:
        """One compact two-line block per GPU: name, then bars + power meta.

        The bars are sized to the box's inner width so the power/temp/clock
        meta is never clipped; when it still does not fit, the least
        important meta (fan, then clock) is dropped.
        """
        out = Text(no_wrap=True)
        # inner width of the box (border + padding)
        inner = max(20, (self.size.width or 64) - 4)
        for i, info in enumerate(snap.gpus):
            if i:
                out.append("\n")
            out.append("▌ ", style=_theme(self, "accent"))
            out.append(f"GPU {info.index}", style=_theme(self, "text_highlight"))
            out.append(f"  {info.name or 'unknown'}", style=_theme(self, "text"))
            out.append("\n")
            out.append("  ", style="")
            util_label = (
                f"util {info.util:5.1f}% " if info.util is not None else "util     —   "
            )
            vram_label = (
                f"   vram {_fmt_bytes(info.mem_used)}/{_fmt_bytes(info.mem_total)} "
                if info.mem_total > 0
                else ""
            )
            meta: list[tuple[str, str]] = []
            if info.power_w is not None:
                # draw / cap: how many watts the GPU uses out of its limit
                if info.power_cap_w:
                    meta.append(
                        (
                            f"{info.power_w:.0f}/{info.power_cap_w:.0f}W",
                            _theme(self, "text_muted"),
                        )
                    )
                else:
                    meta.append((f"{info.power_w:.0f}W", _theme(self, "text_muted")))
            if info.temp_c is not None:
                # color the rounded value so it always matches the display
                meta.append((f"{info.temp_c:.0f}°C", _temp_color(self, round(info.temp_c))))
            if info.clock_mhz is not None:
                meta.append((f"{info.clock_mhz:.0f}MHz", _theme(self, "text_muted")))
            if info.fan_pct is not None:
                meta.append((f"fan {info.fan_pct:.0f}%", _theme(self, "text_muted")))
            # size the bars to the remaining width, dropping the least
            # important meta first when even a minimal bar would not fit
            bars = 2 if info.mem_total > 0 else 1
            while True:
                meta_part = f"   {' · '.join(label for label, _ in meta)}" if meta else ""
                fixed = 2 + len(util_label) + len(vram_label) + len(meta_part)
                bar_w = (inner - fixed) // bars
                if bar_w >= 4 or not meta:
                    break
                meta.pop()
            bar_w = max(1, bar_w)
            out.append(util_label, style=_theme(self, "text"))
            if info.util is not None:
                out.append(_bar(self, info.util, bar_w))
            else:
                out.append(Text("░" * bar_w, style=_theme(self, "text_muted")))
            if info.mem_total > 0:
                pct = 100.0 * info.mem_used / info.mem_total
                out.append(vram_label, style=_theme(self, "text"))
                out.append(_bar(self, pct, bar_w))
            if meta:
                for meta_index, (label, style) in enumerate(meta):
                    if meta_index:
                        out.append(" · ", style=_theme(self, "text_muted"))
                    out.append(label, style=style)
        return out

    def _build_procs(self, snap: GpuSnapshot) -> Text:
        """Unified process list across all GPUs, top N by VRAM usage."""
        entries: list[tuple[str, ProcMem]] = []
        for info in snap.gpus:
            for p in info.procs:
                entries.append((f"GPU {info.index}", p))
        entries.sort(key=lambda e: e[1].mem_bytes, reverse=True)
        entries = entries[: self._procs_limit]
        # the panel's inner width (border + padding), not the list's own
        # content width, so the rows always reach the right border
        width = max(20, (self.size.width or 64) - 4)
        # rows the list may use: box height minus borders and the GPU heads
        height = (self.size.height or 0) - 2 - 2 * len(snap.gpus)
        return _proc_rows(self, entries, width, height=height, header=True)


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------


def _heading(widget: Static, title: str, subtitle: str = "") -> Text:
    """Panel heading: accent '▌' + bold title + optional dim subtitle."""
    out = Text(no_wrap=True)
    out.append("▌", style=_theme(widget, "accent"))
    out.append(" ", style="")
    out.append(title, style=_theme(widget, "text_highlight"))
    if subtitle:
        out.append(f"  {subtitle}", style=_theme(widget, "text_muted"))
    return out


class CpuPanel(Container):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._snap: CpuSnapshot | None = None
        self._body: Static | None = None
        self._rows = CORE_BAR_HEIGHT
        self._cores_per_row = 0
        self._content_lines = 1

    @property
    def content_lines(self) -> int:
        """Rows the panel really used (used by the layout engine)."""
        return self._content_lines

    def compose(self) -> ComposeResult:
        yield Static("", classes="panel-body")

    def on_mount(self) -> None:
        self._body = self.query_one(".panel-body", Static)
        self._refresh()

    def on_resize(self, event) -> None:
        # re-wrap the core grid for the new width immediately
        self._refresh()

    def update_cpu(self, snap: CpuSnapshot, rows: int = CORE_BAR_HEIGHT) -> None:
        self._snap = snap
        self._rows = max(1, rows)
        self._refresh()

    def _refresh(self) -> None:
        if self._body is None:
            return
        self._body.update(self._build_text())

    def _build_text(self) -> Text:
        snap = self._snap
        if snap is None:
            self._content_lines = 1
            return Text("CPU  (waiting…)", style=_theme(self, "text_muted"))
        # the heading must fit the box's inner width: drop load15, then load5,
        # then load1, before giving up on the load values entirely
        inner = max(20, (self.size.width or 64) - 4)
        subtitle = f"{len(snap.cores)} cores · total {snap.total:4.1f}%"
        loads = [f" · load {snap.load1:.2f}", f" {snap.load5:.2f}", f" {snap.load15:.2f}"]
        candidate = subtitle
        for count in (3, 2, 1, 0):
            candidate = subtitle + "".join(loads[:count])
            if 7 + len(candidate) <= inner:  # "▌ CPU  " prefix from _heading
                break
        out = _heading(self, "CPU", candidate)
        out.append("\n")

        cores = snap.cores
        if not cores:
            out.append("(no per-core data yet)", style=_theme(self, "text_muted"))
            self._content_lines = 2
            return out

        dim = _theme(self, "text_muted")
        h = self._rows
        units = 2 * h  # twice the vertical resolution via half-blocks
        # wrap the cores over several lines when the box is too narrow
        per_row = self._cores_per_row or max(1, (self.size.width or 60) // 2)
        chunks = [cores[i : i + per_row] for i in range(0, len(cores), per_row)]
        self._content_lines = 1 + len(chunks) * h
        for chunk_index, chunk in enumerate(chunks):
            if chunk_index:
                out.append("\n")
            # per core: fill level (half-rows) and a bottom-to-top gradient
            ramps = []
            for usage in chunk:
                level = max(0, min(units, int(round(usage / 100 * units))))
                ramps.append((level, color_ramp_from(self.app, _load_color(self, usage), h)))
            for r in range(h - 1, -1, -1):  # top row first
                if r != h - 1:
                    out.append("\n")
                for level, ramp in ramps:
                    if level >= 2 + 2 * r:
                        ch = "█"
                    elif level == 1 + 2 * r:
                        ch = "▄"
                    else:
                        out.append("· ", style=dim)
                        continue
                    out.append(ch + " ", style=ramp[r])
        return out


# ---------------------------------------------------------------------------
# RAM
# ---------------------------------------------------------------------------


class RamPanel(Container):
    """RAM usage with a fixed header and the heaviest processes by RSS."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._snap: RamSnapshot | None = None
        self._head: Static | None = None
        self._procs: Static | None = None
        self._procs_limit = TOP_PROCS
        self._content_lines = 1

    @property
    def content_lines(self) -> int:
        """Rows the panel really used (used by the layout engine)."""
        return self._content_lines

    def compose(self) -> ComposeResult:
        yield Static("", classes="panel-head")
        yield Static("", classes="proc-list")

    def on_mount(self) -> None:
        self._head = self.query_one(".panel-head", Static)
        self._procs = self.query_one(".proc-list", Static)
        self._refresh()

    def on_resize(self, event) -> None:
        # rebuild the process rows for the new size immediately
        self._refresh()

    def update_ram(self, snap: RamSnapshot, procs: int = TOP_PROCS) -> None:
        self._snap = snap
        self._procs_limit = max(0, procs)
        self._refresh()

    def _refresh(self) -> None:
        if self._head is None or self._procs is None:
            return
        snap = self._snap
        if snap is None or snap.total == 0:
            self._head.update(Text("RAM  (waiting…)", style=_theme(self, "text_muted")))
            self._procs.update(Text("", style=_theme(self, "text_muted")))
            return
        self._head.update(self._build_head(snap))
        entries = [("", p) for p in snap.procs[: self._procs_limit]]
        # the panel's inner width (border + padding), not the list's own
        # content width, so the rows always reach the right border
        width = max(20, (self.size.width or 64) - 4)
        # rows the list may use: box height minus borders and the heading
        height = (self.size.height or 0) - 3
        self._procs.update(_proc_rows(self, entries, width, height=height))
        self._content_lines = 1 + len(entries)

    def _build_head(self, snap: RamSnapshot) -> Text:
        pct = 100.0 * snap.used / snap.total
        out = _heading(self, "RAM", f"{_fmt_bytes(snap.used)}/{_fmt_bytes(snap.total)}")
        # shrink the bar to the box's inner width so it never touches the border
        inner = max(20, (self.size.width or 64) - 4)
        bar_w = max(4, inner - len(str(out)) - 2)
        out.append("  ")
        out.append(_bar(self, pct, bar_w, color=_theme(self, "primary")))
        return out


# ---------------------------------------------------------------------------
# model parameters + llama.cpp metrics (one box, seven columns)
# ---------------------------------------------------------------------------


# a metric row: (label, value, style, priority) — higher priority survives longer
MetricRow = tuple[str, str, str, int]
# prepared section: (title, rows, label width, column width)
Section = tuple[str, list[MetricRow], int, int]

GAP = 3  # " │ " between sections


def _prepare_sections(sections: list[tuple[str, list[MetricRow]]]) -> list[Section]:
    """Measure every section: tight label/value columns inside a box wide
    enough for its title."""
    prepared: list[Section] = []
    for title, rows in sections:
        label_w = max((len(label) for label, _, _, _ in rows), default=0)
        val_w = max((len(value) for _, value, _, _ in rows), default=0)
        # at least title + a short rule, so narrow sections don't look broken
        col_w = max(label_w + 2 + val_w, len(title) + 3)
        prepared.append((title, rows, label_w, col_w))
    return prepared


def _row_width(row: list[Section]) -> int:
    return sum(section[3] for section in row) + GAP * (len(row) - 1)


def _pack_sections(
    sections: list[tuple[str, list[MetricRow]]], max_width: int
) -> list[list[Section]]:
    """Arrange the sections into a balanced grid that fits the width.

    The smallest number of rows wins, splitting the sections as evenly as
    possible (7 sections -> 4+3, never 5+2 with a half-empty second row).
    """
    prepared = _prepare_sections(sections)
    if not prepared:
        return []
    count = len(prepared)
    for n_rows in range(1, count + 1):
        base, extra = divmod(count, n_rows)
        grid: list[list[Section]] = []
        index = 0
        for row_index in range(n_rows):
            size = base + (1 if row_index < extra else 0)
            grid.append(prepared[index : index + size])
            index += size
        if max_width <= 0 or all(_row_width(row) <= max_width for row in grid):
            return grid
    return [[section] for section in prepared]


def _grid_height(sections: list[tuple[str, list[MetricRow]]], max_width: int) -> int:
    """Rows needed to render the sections at the given width."""
    grid = _pack_sections(sections, max_width)
    return sum(1 + max(len(rows) for _, rows, _, _ in row) for row in grid)


class MetricsPanel(Container):
    """Everything about the served model in a single box.

    Columns, from left to right:
    MODEL | SAMPLING | TOKENS | SLOTS | CACHE | SPECULATIVE | REQUESTS

    When the terminal is short the panel drops the least important rows
    (by priority) until everything fits in the given budget, so the box
    never overflows its space.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._snap: LlamaSnapshot | None = None
        self._props: ModelProps | None = None
        self._body: Static | None = None
        self._budget = 13
        self._content_lines = 1

    @property
    def content_lines(self) -> int:
        """Rows the panel really used (used by the layout engine)."""
        return self._content_lines

    def compose(self) -> ComposeResult:
        yield Static("", classes="panel-body")

    def on_mount(self) -> None:
        self._body = self.query_one(".panel-body", Static)
        self._refresh()

    def on_resize(self, event) -> None:
        # re-pack the columns for the new width immediately
        self._refresh()

    def update_metrics(
        self,
        snap: LlamaSnapshot | None,
        props: ModelProps | None,
        budget: int = 13,
    ) -> None:
        self._snap = snap
        self._props = props
        self._budget = max(3, budget)
        self._refresh()

    def _refresh(self) -> None:
        if self._body is None:
            return
        self._body.update(self._build_text())

    # ------------------------------------------------------------------
    # content
    # ------------------------------------------------------------------

    def _model_rows(self) -> list[MetricRow]:
        muted = _theme(self, "text_muted")
        accent = _theme(self, "accent")
        props = self._props
        if props is None:
            return [("", "waiting…", muted, 10)]
        return [
            ("model", props.alias or props.model.rsplit("/", 1)[-1] or "—", accent, 10),
            ("quant", props.quant or "—", muted, 9),
            ("ctx", f"{props.n_ctx:,}" if props.n_ctx else "—", muted, 8),
            ("slots", str(props.total_slots) if props.total_slots else "—", muted, 4),
            ("modalities", ", ".join(props.modalities) or "—", muted, 3),
            ("build", props.build or "—", muted, 2),
        ]

    def _sampling_rows(self) -> list[MetricRow]:
        muted = _theme(self, "text_muted")
        accent = _theme(self, "accent")
        props = self._props
        rows: list[MetricRow] = []
        if props is not None:
            if props.speculative_types and props.speculative_types != "none":
                rows.append(("spec", props.speculative_types, accent, 7))
            for label, value in props.sampling:
                priority = {
                    "temp": 8,
                    "top_k": 6,
                    "top_p": 6,
                    "min_p": 3,
                    "repeat pen": 5,
                    "max tokens": 4,
                }.get(label, 3)
                rows.append((label, value, muted, priority))
        if not rows:
            rows = [("", "—", muted, 3)]
        return rows

    def _build_text(self) -> Text:
        snap = self._snap
        muted = _theme(self, "text_muted")
        accent = _theme(self, "accent")
        text = _theme(self, "text")

        if snap is None and self._props is None:
            self._content_lines = 1
            return Text("MODEL & METRICS  (waiting…)", style=muted)

        token_rows: list[MetricRow] = [("", "offline", muted, 1)]
        slot_rows: list[MetricRow] = []
        cache_rows: list[MetricRow] = [("", "offline", muted, 1)]
        spec_rows: list[MetricRow] = [("", "disabled", muted, 1)]
        request_rows: list[MetricRow] = [("", "offline", muted, 1)]

        if snap is not None and snap.reachable:
            token_rows = [
                ("generated", _fmt_int(snap.tokens_predicted_total), text, 10),
                ("prompt", _fmt_int(snap.prompt_tokens_total), text, 8),
                ("cached", _fmt_int(snap.prompt_tokens_cached_total), muted, 6),
            ]

            # live per-slot state: one labelled line per metric so the column
            # reads as a small table instead of blank-label continuation rows
            for s in snap.slots:
                if not s.processing:
                    continue
                slot_rows.append((f"slot {s.id}", f"task {s.task}", text, 9))
                slot_rows.append(("dec", str(s.decoded), text, 9))
                if s.remain > 0:
                    slot_rows.append(("left", str(s.remain), muted, 4))
                if s.prompt_tokens:
                    slot_rows.append(
                        ("pre", f"{s.prompt_processed}/{s.prompt_tokens}", muted, 6)
                    )
                if s.prompt_cached:
                    slot_rows.append(("cached", str(s.prompt_cached), muted, 3))
            if not slot_rows:
                slot_rows = [("", "idle", muted, 1)]

            cache_rows = [
                (
                    "hit rate",
                    f"{100 * snap.cache_hit_rate:.1f}%"
                    if snap.cache_hit_rate is not None
                    else "—",
                    accent if snap.cache_hit_rate is not None else muted,
                    8,
                )
            ]

            if snap.spec is not None:
                spec_rows = [
                    ("accept", f"{100 * snap.spec.accept_rate:.1f}%", accent, 8),
                    ("avg/step", f"{snap.spec.avg_per_step:.2f}", text, 6),
                ]
                for pos in sorted(snap.spec.per_pos):
                    spec_rows.append(
                        (f"pos{pos}", f"{100 * snap.spec.per_pos[pos]:.1f}%", muted, 3)
                    )

            request_rows = [
                ("processing", str(snap.requests_processing), text, 7),
                ("deferred", str(snap.requests_deferred), text, 4),
                (
                    "busy slots",
                    f"{snap.busy_slots:.0f}" if snap.busy_slots is not None else "—",
                    muted,
                    6,
                ),
            ]

        sections: list[tuple[str, list[MetricRow]]] = [
            ("MODEL", self._model_rows()),
            ("SAMPLING", self._sampling_rows()),
            ("TOKENS", token_rows),
            ("SLOTS", slot_rows),
            ("CACHE", cache_rows),
            ("SPECULATIVE", spec_rows),
            ("REQUESTS", request_rows),
        ]

        width = max(60, (self.size.width or 120) - 2)
        sections = self._compact(sections, width)
        self._content_lines = _grid_height(sections, width)
        return self._render_sections(sections, width)

    # ------------------------------------------------------------------
    # compaction: drop the least important rows until the box fits
    # ------------------------------------------------------------------

    def _compact(
        self, sections: list[tuple[str, list[MetricRow]]], width: int
    ) -> list[tuple[str, list[MetricRow]]]:
        rows = [(title, list(rows)) for title, rows in sections]
        while _grid_height(rows, width) > self._budget:
            # find the lowest priority row (a section always keeps one row)
            worst: tuple[int, int] | None = None
            for index, (_, section_rows) in enumerate(rows):
                if len(section_rows) <= 1:
                    continue
                for row_index, row in enumerate(section_rows):
                    if worst is None or row[3] < worst[0]:
                        worst = (row[3], index * 1000 + row_index)
            if worst is None:
                break
            priority, flat = worst
            index, row_index = divmod(flat, 1000)
            del rows[index][1][row_index]
        return rows

    def _render_sections(self, sections: list[tuple[str, list[MetricRow]]], width: int) -> Text:
        muted = _theme(self, "text_muted")
        title_style = f"{_theme(self, 'primary')} bold"
        rule_style = _theme(self, "text_muted")
        grid = _pack_sections(sections, width)

        out = Text(no_wrap=True)
        for grid_index, grid_row in enumerate(grid):
            if grid_index:
                out.append("\n")
            # one row for the titles, then one per value row
            n_rows = 1 + max(len(rows) for _, rows, _, _ in grid_row)
            for r in range(n_rows):
                if r:
                    out.append("\n")
                for c, (title, rows, label_w, col_w) in enumerate(grid_row):
                    if c:
                        out.append(" ", style="")
                        out.append("│", style=rule_style)
                        out.append(" ", style="")
                    if r == 0:
                        out.append(title, style=title_style)
                        rule_w = col_w - len(title) - 1
                        if rule_w > 0:
                            out.append(" ", style="")
                            out.append("─" * rule_w, style=rule_style)
                    elif r - 1 < len(rows):
                        label, value, style, _ = rows[r - 1]
                        out.append(f"{label:<{label_w}}", style=muted)
                        out.append("  ", style="")
                        # value right-aligned: the column reads as a table
                        out.append(
                            " " * (col_w - label_w - 2 - len(value)), style=""
                        )
                        out.append(value, style=style)
                    else:
                        out.append(" " * col_w, style="")
        return out
