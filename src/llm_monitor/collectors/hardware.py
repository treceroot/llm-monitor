"""Hardware collectors: GPU (NVML), CPU and RAM (/proc)."""
from __future__ import annotations

import glob

from .types import CpuSnapshot, GpuInfo, GpuSnapshot, ProcMem, RamSnapshot

_PAGE_SIZE = 4096


def _proc_name(pid: int) -> str:
    """Best-effort process name from /proc/<pid>/comm."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except Exception:
        return f"pid {pid}"

# ---------------------------------------------------------------------------
# GPU (NVML)
# ---------------------------------------------------------------------------

_nvml = None
_handles: list = []


def gpu_poll() -> GpuSnapshot:
    """Synchronous NVML poll; run via asyncio.to_thread()."""
    global _nvml, _handles
    snap = GpuSnapshot()
    try:
        if _nvml is None:
            import pynvml

            pynvml.nvmlInit()
            _nvml = pynvml
            _handles = [
                pynvml.nvmlDeviceGetHandleByIndex(i)
                for i in range(pynvml.nvmlDeviceGetCount())
            ]
        pynvml = _nvml
        for i, h in enumerate(_handles):
            info = GpuInfo(index=i)
            try:
                info.name = pynvml.nvmlDeviceGetName(h)
            except Exception:
                pass
            try:
                info.util = float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
            except Exception:
                pass
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                info.mem_used = mem.used
                info.mem_total = mem.total
            except Exception:
                pass
            try:
                info.power_w = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
            except Exception:
                pass
            try:
                info.power_cap_w = pynvml.nvmlDeviceGetPowerManagementLimit(h) / 1000.0
            except Exception:
                pass
            try:
                info.temp_c = float(
                    pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
                )
            except Exception:
                pass
            try:
                info.clock_mhz = float(
                    pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)
                )
            except Exception:
                pass
            try:
                info.fan_pct = float(pynvml.nvmlDeviceGetFanSpeed(h))
            except Exception:
                pass
            info.procs = _gpu_procs(pynvml, h)
            snap.gpus.append(info)
    except Exception as exc:
        snap.ok = False
        snap.error = str(exc)
    return snap


def _gpu_procs(pynvml, handle) -> list[ProcMem]:
    """Per-process VRAM on a device: compute + graphics processes, deduped by pid.

    Some drivers report the same pid in both lists (with different slices of
    memory); we sum them. Sorted by VRAM usage, descending.
    """
    by_pid: dict[int, int] = {}
    for getter in (
        pynvml.nvmlDeviceGetComputeRunningProcesses,
        pynvml.nvmlDeviceGetGraphicsRunningProcesses,
    ):
        try:
            for p in getter(handle):
                by_pid[p.pid] = by_pid.get(p.pid, 0) + (p.usedGpuMemory or 0)
        except Exception:
            continue
    procs = [
        ProcMem(pid=pid, name=_proc_name(pid), mem_bytes=mem)
        for pid, mem in by_pid.items()
        if mem > 0
    ]
    procs.sort(key=lambda p: p.mem_bytes, reverse=True)
    return procs


# ---------------------------------------------------------------------------
# CPU (/proc/stat)
# ---------------------------------------------------------------------------

_prev_stat: Optional[list[tuple[str, tuple[int, ...]]]] = None


def _read_stat() -> list[tuple[str, tuple[int, ...]]]:
    lines: list[tuple[str, tuple[int, ...]]] = []
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu"):
                parts = line.split()
                # user nice system idle iowait irq softirq steal
                lines.append((parts[0], tuple(int(x) for x in parts[1:9])))
    return lines


def cpu_poll() -> CpuSnapshot:
    global _prev_stat
    snap = CpuSnapshot()
    try:
        cur = _read_stat()
        load = open("/proc/loadavg").read().split()
        snap.load1, snap.load5, snap.load15 = (
            float(load[0]),
            float(load[1]),
            float(load[2]),
        )
        if _prev_stat is not None:
            prev_map = dict(_prev_stat)
            for name, vals in cur:
                pv = prev_map.get(name)
                if pv is None:
                    continue
                total = sum(vals) - sum(pv)
                idle = (vals[3] + vals[4]) - (pv[3] + pv[4])
                usage = 100.0 * (total - idle) / total if total > 0 else 0.0
                if name == "cpu":
                    snap.total = usage
                else:
                    snap.cores.append(usage)
        _prev_stat = cur
    except Exception:
        pass
    return snap


# ---------------------------------------------------------------------------
# RAM (/proc/meminfo)
# ---------------------------------------------------------------------------


def ram_procs() -> list[ProcMem]:
    """Per-process RSS for every readable /proc/<pid>, sorted by memory desc.

    Reads statm (resident pages) and comm. Processes that vanish mid-scan or
    are unreadable are skipped.
    """
    procs: list[ProcMem] = []
    for d in glob.glob("/proc/[0-9]*"):
        pid = int(d.rsplit("/", 1)[-1])
        try:
            with open(f"{d}/statm") as f:
                resident = int(f.read().split()[1])
            with open(f"{d}/comm") as f:
                name = f.read().strip()
        except Exception:
            continue
        mem = resident * _PAGE_SIZE
        if mem > 0:
            procs.append(ProcMem(pid=pid, name=name, mem_bytes=mem))
    procs.sort(key=lambda p: p.mem_bytes, reverse=True)
    return procs


def ram_poll() -> RamSnapshot:
    snap = RamSnapshot()
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    info[key] = int(parts[0])  # kB
        snap.total = info.get("MemTotal", 0) * 1024
        avail = info.get("MemAvailable", 0) * 1024
        snap.used = max(0, snap.total - avail)
        snap.swap_total = info.get("SwapTotal", 0) * 1024
        snap.swap_used = max(0, snap.swap_total - info.get("SwapFree", 0) * 1024)
    except Exception:
        pass
    try:
        snap.procs = ram_procs()
    except Exception:
        pass
    return snap
