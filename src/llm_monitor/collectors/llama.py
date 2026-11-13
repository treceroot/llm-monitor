"""Collector for llama.cpp server metrics (Prometheus endpoint + /props)."""
from __future__ import annotations

import re
import time
from typing import Optional

import httpx

from .types import LlamaSnapshot, ModelProps, SlotInfo, SpecStats

_LINE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eEanif]+)\s*$"
)


def parse_prometheus(
    text: str,
) -> dict[str, list[tuple[dict[str, str], float]]]:
    """Parse Prometheus exposition format into {name: [(labels, value), ...]}.

    A name may appear multiple times with different label sets (e.g.
    per-position speculative decoding counters); all series are kept.
    """
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        name, labels_s, value_s = m.group(1), m.group(2) or "", m.group(3)
        try:
            value = float(value_s)
        except ValueError:
            continue
        labels: dict[str, str] = {}
        if labels_s:
            for part in labels_s[1:-1].split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    labels[k.strip()] = v.strip().strip('"')
        out.setdefault(name, []).append((labels, value))
    return out


def _fmt_val(v) -> Optional[str]:
    """Compact display value for a /props parameter."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        if abs(v - round(v)) < 1e-6:
            return str(int(round(v)))
        return f"{v:g}"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) if v else None
    s = str(v).strip()
    return s if s else None


class LlamaCollector:
    """Polls /metrics and /props, computing instantaneous rates from counters."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._last: dict[str, float] = {}
        self._last_ts: Optional[float] = None
        # slot id -> (id_task, n_decoded, n_prompt_tokens_processed) from the
        # last /slots poll; used to compute live per-slot rates
        self._last_slot: dict[int, tuple[int, int, int]] = {}
        # None = unknown, False = /slots disabled on this server (HTTP error),
        # True = endpoint available
        self._slots_ok: Optional[bool] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=2.0)
        return self._client

    async def _slot_rates(
        self, client: httpx.AsyncClient, dt: float
    ) -> tuple[Optional[float], Optional[float], list[SlotInfo]]:
        """Live per-slot rates from /slots (requires --slots on the server).

        Returns (decode_tps, prefill_tps, slot_infos). Rates are deltas of
        each slot's n_decoded / n_prompt_tokens_processed between polls,
        guarded by id_task so per-task resets are not mistaken for rates.
        """
        if self._slots_ok is False:
            return None, None, []
        try:
            resp = await client.get("/slots")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError:
            # endpoint disabled on this server (needs --slots): stop asking
            self._slots_ok = False
            return None, None, []
        except Exception:
            # transport hiccup: retry next tick
            return None, None, []
        self._slots_ok = True

        slots_raw = data if isinstance(data, list) else data.get("slots", [])
        dec_rate = 0.0
        pre_rate = 0.0
        any_dec = False
        any_pre = False
        infos: list[SlotInfo] = []
        seen: set[int] = set()
        for s in slots_raw:
            try:
                sid = int(s.get("id", 0))
                task = int(s.get("id_task", -1))
                next_tokens = s.get("next_token") or []
                ndec = sum(int(t.get("n_decoded", 0) or 0) for t in next_tokens)
                remain = int(next_tokens[0].get("n_remain", 0) or 0) if next_tokens else 0
                npre = int(s.get("n_prompt_tokens_processed", 0) or 0)
                info = SlotInfo(
                    id=sid,
                    task=task,
                    processing=bool(s.get("is_processing")),
                    prompt_tokens=int(s.get("n_prompt_tokens", 0) or 0),
                    prompt_processed=npre,
                    prompt_cached=int(s.get("n_prompt_tokens_cache", 0) or 0),
                    decoded=ndec,
                    remain=remain,
                )
            except (TypeError, ValueError, AttributeError, IndexError):
                continue
            infos.append(info)
            seen.add(sid)

            prev = self._last_slot.get(sid)
            if prev is not None and prev[0] == task and dt > 0:
                # same task: deltas are meaningful
                if ndec >= prev[1]:
                    dec_rate += (ndec - prev[1]) / dt
                    any_dec = True
                if npre >= prev[2]:
                    pre_rate += (npre - prev[2]) / dt
                    any_pre = True
            self._last_slot[sid] = (task, ndec, npre)
        for sid in [k for k in self._last_slot if k not in seen]:
            del self._last_slot[sid]

        return (
            dec_rate if any_dec else None,
            pre_rate if any_pre else None,
            infos,
        )

    async def poll(self) -> LlamaSnapshot:
        snap = LlamaSnapshot()
        try:
            client = await self._get_client()
            resp = await client.get("/metrics")
            resp.raise_for_status()
            metrics = parse_prometheus(resp.text)
        except Exception as exc:
            snap.reachable = False
            snap.error = str(exc)
            return snap

        snap.reachable = True
        now = time.monotonic()
        dt = (now - self._last_ts) if self._last_ts is not None else 0.0

        def counter(name: str) -> Optional[float]:
            entries = metrics.get(name)
            return entries[0][1] if entries else None

        def rate(name: str) -> Optional[float]:
            """Instantaneous rate from a monotonic counter; None on first sample or reset."""
            cur = counter(name)
            if cur is None:
                return None
            r: Optional[float] = None
            prev = self._last.get(name)
            if prev is not None and dt > 0 and cur >= prev:
                r = (cur - prev) / dt
            self._last[name] = cur
            return r

        def gauge(name: str) -> Optional[float]:
            entries = metrics.get(name)
            return entries[0][1] if entries else None

        # Instantaneous t/s, in order of preference:
        #   1. /slots deltas — truly live, per token (requires --slots)
        #   2. *_tokens_seconds gauges — recent average updated per request
        #   3. Prometheus counter deltas — only move when a task finishes
        slot_dec, slot_pre, slot_infos = await self._slot_rates(client, dt)
        snap.slots = slot_infos
        c_dec = rate("llamacpp:tokens_predicted_total")
        c_pre = rate("llamacpp:prompt_tokens_total")
        g_dec = gauge("llamacpp:predicted_tokens_seconds")
        g_pre = gauge("llamacpp:prompt_tokens_seconds")

        def pick(live, recent, counter):
            if live is not None:
                return live
            if recent is not None and recent > 0:
                return recent
            return counter

        snap.decode_tps = pick(slot_dec, g_dec, c_dec)
        snap.prefill_tps = pick(slot_pre, g_pre, c_pre)

        snap.tokens_predicted_total = int(gauge("llamacpp:tokens_predicted_total") or 0)
        snap.prompt_tokens_total = int(gauge("llamacpp:prompt_tokens_total") or 0)
        snap.prompt_tokens_cached_total = int(
            gauge("llamacpp:prompt_tokens_cached_total") or 0
        )

        # session averages (like the llama.cpp CLI tg/pp): total tokens /
        # total time — they persist while the server is idle, unlike the
        # server's rolling *_tokens_seconds gauges which decay to 0
        gen_seconds = gauge("llamacpp:tokens_predicted_seconds_total")
        if gen_seconds and gen_seconds > 0:
            snap.decode_avg_tps = snap.tokens_predicted_total / gen_seconds
        prompt_seconds = gauge("llamacpp:prompt_seconds_total")
        if prompt_seconds and prompt_seconds > 0:
            snap.prefill_avg_tps = snap.prompt_tokens_total / prompt_seconds
        processed = snap.prompt_tokens_total
        cached = snap.prompt_tokens_cached_total
        if processed + cached > 0:
            snap.cache_hit_rate = cached / (processed + cached)

        snap.requests_processing = int(gauge("llamacpp:requests_processing") or 0)
        snap.requests_deferred = int(gauge("llamacpp:requests_deferred") or 0)
        snap.busy_slots = gauge("llamacpp:n_busy_slots_per_decode")

        # speculative decoding (session cumulative ratios)
        drafts = counter("llamacpp:spec_decode_num_drafts_total")
        draft_tokens = counter("llamacpp:spec_decode_num_draft_tokens_total")
        accepted = counter("llamacpp:spec_decode_num_accepted_tokens_total")
        if drafts and drafts > 0 and accepted is not None:
            spec = SpecStats(
                drafts_total=int(drafts),
                accepted_total=int(accepted),
                avg_per_step=1.0 + accepted / drafts,
            )
            if draft_tokens and draft_tokens > 0:
                spec.draft_tokens_total = int(draft_tokens)
                spec.accept_rate = accepted / draft_tokens
            else:
                spec.accept_rate = accepted / drafts
            for labels, value in metrics.get(
                "llamacpp:spec_decode_num_accepted_tokens_per_pos_total", []
            ):
                pos = labels.get("position")
                if pos is not None:
                    try:
                        spec.per_pos[int(pos)] = value / drafts
                    except ValueError:
                        pass
            snap.spec = spec

        self._last_ts = now
        return snap

    async def fetch_props(self) -> LlamaSnapshot:
        """Fetch /props for model info; returns a snapshot with only those fields."""
        snap = LlamaSnapshot()
        try:
            client = await self._get_client()
            resp = await client.get("/props")
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return snap
        snap.reachable = True
        snap.alias = data.get("model_alias") or ""
        snap.model = data.get("model_path") or ""

        gen = data.get("default_generation_settings") or {}
        params = gen.get("params") or {}
        snap.n_ctx = _fmt_val(gen.get("n_ctx")) and int(gen["n_ctx"])

        props = ModelProps(
            alias=snap.alias,
            model=snap.model,
            quant=data.get("model_ftype") or "",
            build=data.get("build_info") or "",
            n_ctx=snap.n_ctx,
            total_slots=int(data.get("total_slots", 0) or 0),
            speculative_types=_fmt_val(params.get("speculative.types")) or "",
        )
        mods = data.get("modalities") or {}
        props.modalities = [k for k, v in mods.items() if v]

        # curated sampling defaults shown in the MODEL panel
        for label, value in (
            ("temp", params.get("temperature")),
            ("top_k", params.get("top_k")),
            ("top_p", params.get("top_p")),
            ("min_p", params.get("min_p")),
            ("repeat pen", params.get("repeat_penalty")),
            ("mirostat", params.get("mirostat") if params.get("mirostat") else None),
            ("max tokens", params.get("n_predict")),
            ("samplers", params.get("samplers")),
        ):
            v = _fmt_val(value)
            if v is not None:
                if label == "max tokens" and v == "-1":
                    v = "inf"
                props.sampling.append((label, v))
        snap.props = props
        return snap

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
