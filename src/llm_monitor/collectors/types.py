"""Dataclasses shared by collectors and widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SpecStats:
    """Speculative decoding stats (session cumulative)."""

    drafts_total: int = 0           # verification steps
    draft_tokens_total: int = 0     # draft tokens proposed
    accepted_total: int = 0
    accept_rate: float = 0.0        # accepted / draft tokens proposed
    avg_per_step: float = 0.0       # 1 + accepted / steps
    per_pos: dict[int, float] = field(default_factory=dict)  # position -> fraction of steps accepted


@dataclass
class SlotInfo:
    """Live state of one server slot (from /slots, when enabled)."""

    id: int = 0
    task: int = -1                     # id_task of the current job
    processing: bool = False
    prompt_tokens: int = 0             # total prompt tokens for the task
    prompt_processed: int = 0          # prompt tokens processed so far
    prompt_cached: int = 0             # prompt tokens served from KV cache
    decoded: int = 0                   # tokens generated so far
    remain: int = 0                    # tokens left (n_predict budget)


@dataclass
class ModelProps:
    """Static model / server parameters (from /props)."""

    alias: str = ""
    model: str = ""
    quant: str = ""                    # e.g. "Q4_K - Medium" (model_ftype)
    build: str = ""                    # e.g. "b11706-f74e2fa6a"
    n_ctx: Optional[int] = None
    total_slots: int = 0
    modalities: list[str] = field(default_factory=list)   # ["vision", "video", ...]
    speculative_types: str = ""
    sampling: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class LlamaSnapshot:
    reachable: bool = False
    error: str = ""

    # model / server info
    alias: str = ""
    model: str = ""
    n_ctx: Optional[int] = None
    uptime_seconds: Optional[float] = None
    props: Optional[ModelProps] = None

    # live per-slot state
    slots: list[SlotInfo] = field(default_factory=list)

    # instantaneous rates (delta of counters between polls)
    decode_tps: Optional[float] = None
    prefill_tps: Optional[float] = None
    # session averages (gauges from the server)
    decode_avg_tps: Optional[float] = None
    prefill_avg_tps: Optional[float] = None

    # cumulative counters
    tokens_predicted_total: int = 0
    prompt_tokens_total: int = 0
    prompt_tokens_cached_total: int = 0
    cache_hit_rate: Optional[float] = None

    # requests
    requests_processing: int = 0
    requests_deferred: int = 0
    busy_slots: Optional[float] = None

    spec: Optional[SpecStats] = None


@dataclass
class ProcMem:
    """A process and how much memory it holds (RSS for RAM, VRAM for GPU)."""

    pid: int
    name: str
    mem_bytes: int


@dataclass
class GpuInfo:
    index: int
    name: str = ""
    util: Optional[float] = None          # %
    mem_used: int = 0                     # bytes
    mem_total: int = 0                    # bytes
    power_w: Optional[float] = None
    power_cap_w: Optional[float] = None   # power management limit (TDP cap)
    temp_c: Optional[float] = None
    clock_mhz: Optional[float] = None
    fan_pct: Optional[float] = None
    procs: list[ProcMem] = field(default_factory=list)  # per-process VRAM


@dataclass
class GpuSnapshot:
    ok: bool = True
    error: str = ""
    gpus: list[GpuInfo] = field(default_factory=list)


@dataclass
class CpuSnapshot:
    total: float = 0.0                    # %
    cores: list[float] = field(default_factory=list)  # % per core
    load1: float = 0.0
    load5: float = 0.0
    load15: float = 0.0


@dataclass
class RamSnapshot:
    total: int = 0                        # bytes
    used: int = 0                         # bytes
    swap_total: int = 0
    swap_used: int = 0
    procs: list[ProcMem] = field(default_factory=list)  # per-process RSS
