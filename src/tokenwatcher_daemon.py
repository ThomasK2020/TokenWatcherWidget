#!/usr/bin/env python3
"""
Démon Principal TokenWatcher V4.2.4.
Orchestre les 3 sondes (AmdSysfsProbe, EnginesHttpProbe, SqliteTelemetryReader)
à un intervalle de 1.0s et écrit atomiquement l'état dans /tmp/tokenwatcher_widget_state.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from probes.amd_sysfs import AmdSysfsProbe
from probes.engines_http import EnginesHttpProbe
from probes.sqlite_readers import SqliteTelemetryReader

DEFAULT_PORTS_CONFIG: Dict[str, Any] = {
    "services": {
        "proxy": {"host": "127.0.0.1", "port": 13306, "unit": "lemonade-opencode-proxy.service"},
        "vulkan": {"host": "127.0.0.1", "port": 8001, "slots_endpoint": "/slots", "metrics_endpoint": "/metrics"},
        "lemonade_admin": {"host": "127.0.0.1", "port": 13305, "health_endpoint": "/api/v1/health"},
        "vllm_rocm": {"host": "127.0.0.1", "port": 8000, "metrics_endpoint": "/metrics", "models_endpoint": "/v1/models"},
        "open_webui": {"host": "127.0.0.1", "port": 8080},
        "ollama": {"host": "127.0.0.1", "port": 11434, "ps_endpoint": "/api/ps"},
    },
    "hermes": {
        "active_profile": "auto",
        "include_all_profiles": True,
        "active_session_window_sec": 120,
        "max_sessions": 25,
    },
    "cloud_quotas": {
        "default": {"label": "Cloud API", "tpm_ceiling": 2000000, "rpm_ceiling": 1000, "rpd_ceiling": 10000},
        "models": {},
    },
    "ipc": {
        "state_file": "/tmp/tokenwatcher_widget_state.json",
        "polling_interval_sec": 1.0,
    },
    "efficiency": {
        "electricity_eur_per_kwh": 0.20,
        "discrete_ref_tok_per_j": 0.28,
        "discrete_ref_label": "GPU discret 300W (~0.28 tok/J)",
    },
    "power": {
        "idle_baseline_watts": "auto",
        "fallback_idle_watts": 12.0,
        "idle_max_gpu_busy_pct": 5,
        "idle_max_cpu_busy_pct": 15,
        "idle_min_window_sec": 15,
        "baseline_cache_file": "~/.cache/tokenwatcher/power_baseline.json",
    },
}


class TokenWatcherDaemon:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.sysfs_probe = AmdSysfsProbe()
        self.sqlite_reader = SqliteTelemetryReader()
        self.engines_probe: EnginesHttpProbe | None = None
        self._full_config: Dict[str, Any] = {}
        self._config_file_name: str = "tokenwatcher.yaml"
        self._last_active_efficiency: float = 0.0
        self._last_active_tg_speed: float = 0.0
        self._last_active_watts: float = 0.0
        self._last_active_model: str = ""
        self._eff_ema: Optional[float] = None
        self._peak_efficiency: float = 0.0
        self._history_efficiency: List[float] = []

    def load_ports_config(self) -> Dict[str, Any]:
        """Charger config/tokenwatcher.yaml (avec repli rétrocompatible sur config/ports.yaml)."""
        merged = {
            "services": {k: dict(v) for k, v in DEFAULT_PORTS_CONFIG["services"].items()},
            "hermes": dict(DEFAULT_PORTS_CONFIG["hermes"]),
            "cloud_quotas": {
                "default": dict(DEFAULT_PORTS_CONFIG["cloud_quotas"]["default"]),
                "models": dict(DEFAULT_PORTS_CONFIG["cloud_quotas"]["models"]),
            },
            "ipc": dict(DEFAULT_PORTS_CONFIG["ipc"]),
            "efficiency": dict(DEFAULT_PORTS_CONFIG["efficiency"]),
            "power": dict(DEFAULT_PORTS_CONFIG["power"]),
        }
        primary_file = self.config_dir / "tokenwatcher.yaml"
        legacy_file = self.config_dir / "ports.yaml"
        cfg_file = primary_file if primary_file.is_file() else (legacy_file if legacy_file.is_file() else primary_file)
        self._config_file_name = cfg_file.name
        if cfg_file.is_file():
            try:
                loaded = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
                if isinstance(loaded.get("services"), dict):
                    for k, v in loaded["services"].items():
                        if isinstance(v, dict):
                            merged["services"].setdefault(k, {}).update(v)
                if isinstance(loaded.get("hermes"), dict):
                    merged["hermes"].update(loaded["hermes"])
                if isinstance(loaded.get("cloud_quotas"), dict):
                    cq = loaded["cloud_quotas"]
                    if isinstance(cq.get("default"), dict):
                        merged["cloud_quotas"]["default"].update(cq["default"])
                    if isinstance(cq.get("models"), dict):
                        merged["cloud_quotas"]["models"].update(cq["models"])
                if isinstance(loaded.get("ipc"), dict):
                    merged["ipc"].update(loaded["ipc"])
                if isinstance(loaded.get("efficiency"), dict):
                    merged["efficiency"].update(loaded["efficiency"])
                if isinstance(loaded.get("power"), dict):
                    merged["power"].update(loaded["power"])
                if isinstance(loaded.get("inference_routing"), dict):
                    merged["inference_routing"] = dict(loaded["inference_routing"])
            except Exception as exc:
                print(f"Avertissement lecture {cfg_file.name}: {exc}")

        self._full_config = merged
        self.sysfs_probe.configure_power(merged.get("power"))
        self.sqlite_reader.configure(
            hermes_cfg=merged["hermes"],
            quotas_cfg=merged["cloud_quotas"],
            routing_cfg=merged.get("inference_routing"),
        )
        self.engines_probe = EnginesHttpProbe(merged)
        return merged

    async def collect_once(self) -> Dict[str, Any]:
        """Fusionner les données des 3 sondes dans le schéma JSON V4.2.4."""
        if not self._full_config or self.engines_probe is None:
            self.load_ports_config()

        assert self.engines_probe is not None

        sysfs_raw, sqlite_data, vulkan_data, vllm_data, ollama_instances = await asyncio.gather(
            asyncio.to_thread(self.sysfs_probe.read_metrics),
            asyncio.to_thread(self.sqlite_reader.read_active_topology_and_tokens),
            self.engines_probe.poll_vulkan_slots(),
            self.engines_probe.poll_vllm_metrics(),
            self.engines_probe.poll_ollama_models(),
        )

        meta = sysfs_raw.pop("_meta", {})
        services = self._full_config["services"]

        # Arbitrage topologie avec état live des moteurs HTTP
        active_topology = int(sqlite_data.get("active_topology", 1))
        if vllm_data.get("online") and int(vllm_data.get("running_reqs", 0)) > 0:
            active_topology = 3
        elif vulkan_data.get("online") and active_topology == 4:
            # Si Vulkan local est actif et une session récente existe
            oc_ts = sqlite_data.get("opencode", {}).get("latest_ts", 0.0)
            hm_ts = sqlite_data.get("hermes", {}).get("latest_ts", 0.0)
            if oc_ts > 0 and hm_ts > 0 and abs(hm_ts - oc_ts) < 900:
                active_topology = 1

        is_rocm = active_topology == 3 and vllm_data.get("online")
        tg_speed = float(vllm_data["speed_tps"] if is_rocm else vulkan_data.get("speed_tps", 0.0))
        pp_speed = int(vulkan_data.get("speed_pp_tps", 0))
        ttft_sec = float(vllm_data["avg_ttft_sec"] if is_rocm else vulkan_data.get("ttft_sec", 0.0))

        # Efficacité énergétique réelle tok/J (latch en fenêtre active pour éviter le biais IDLE)
        gfx_w = float(sysfs_raw.get("gfx_power_w", 0.0))
        eff_cfg = self._full_config.get("efficiency") or DEFAULT_PORTS_CONFIG["efficiency"]
        eur_per_kwh = float(eff_cfg.get("electricity_eur_per_kwh", 0.20))
        discrete_ref = float(eff_cfg.get("discrete_ref_tok_per_j", 0.28))

        is_generating = bool(
            (is_rocm and int(vllm_data.get("running_reqs", 0)) > 0)
            or (
                not is_rocm
                and (
                    int(vulkan_data.get("slots_active", 0)) > 0
                    or "RUN" in str(vulkan_data.get("status_label", ""))
                )
            )
        )
        any_local_generating = bool(
            is_generating
            or int(vllm_data.get("running_reqs", 0)) > 0
            or int(vulkan_data.get("slots_active", 0)) > 0
            or "RUN" in str(vulkan_data.get("status_label", ""))
        )
        self.sysfs_probe.finalize_power_baseline(sysfs_raw, engines_active=any_local_generating)
        eff_model_hint = str(
            (vllm_data.get("model") if is_rocm else vulkan_data.get("model")) or ""
        )

        if is_generating and gfx_w > 0 and tg_speed > 0:
            inst_eff = tg_speed / gfx_w
            self._eff_ema = inst_eff if self._eff_ema is None else (0.45 * inst_eff + 0.55 * self._eff_ema)
            eff_val = round(self._eff_ema, 2)
            self._last_active_efficiency = eff_val
            self._last_active_tg_speed = round(tg_speed, 1)
            self._last_active_watts = round(gfx_w, 1)
            self._last_active_model = eff_model_hint
            eff_source = "live"
            eff_tg = self._last_active_tg_speed
            eff_watts = self._last_active_watts
            eff_model = self._last_active_model
            self._history_efficiency.append(eff_val)
            self._history_efficiency = self._history_efficiency[-16:]
        elif self._last_active_efficiency > 0:
            eff_val = self._last_active_efficiency
            eff_source = "last_run"
            eff_tg = self._last_active_tg_speed
            eff_watts = self._last_active_watts
            eff_model = self._last_active_model
        elif gfx_w > 0 and tg_speed > 0:
            eff_val = round(tg_speed / gfx_w, 2)
            eff_source = "session_avg"
            eff_tg = round(tg_speed, 1)
            eff_watts = round(gfx_w, 1)
            eff_model = eff_model_hint
            if not self._history_efficiency:
                self._history_efficiency.append(eff_val)
        else:
            eff_val = 0.0
            eff_source = "idle"
            eff_tg = 0.0
            eff_watts = round(gfx_w, 1)
            eff_model = eff_model_hint

        if eff_val > 0:
            self._peak_efficiency = max(self._peak_efficiency, eff_val)

        if eff_val >= 1.50:
            eff_tier = "ULTRA"
        elif eff_val >= 0.80:
            eff_tier = "GOLD"
        elif eff_val >= 0.35:
            eff_tier = "EFFICIENT"
        elif eff_val > 0:
            eff_tier = "BASELINE"
        else:
            eff_tier = "IDLE"

        joules_per_tok = round(1.0 / eff_val, 2) if eff_val > 0 else 0.0
        kwh_per_1m = round((1_000_000.0 / eff_val) / 3_600_000.0, 3) if eff_val > 0 else 0.0
        eur_per_1m = round(kwh_per_1m * eur_per_kwh, 3) if eff_val > 0 else 0.0
        vs_discrete = round(eff_val / discrete_ref, 1) if (eff_val > 0 and discrete_ref > 0) else 0.0

        sysfs_raw["efficiency_tok_per_j"] = eff_val
        sysfs_raw["efficiency_source"] = eff_source
        sysfs_raw["efficiency_tier"] = eff_tier
        sysfs_raw["efficiency_tg_tps"] = eff_tg
        sysfs_raw["efficiency_watts"] = eff_watts
        sysfs_raw["efficiency_model"] = eff_model
        sysfs_raw["efficiency_peak_tok_per_j"] = round(self._peak_efficiency, 2)
        sysfs_raw["joules_per_token"] = joules_per_tok
        sysfs_raw["kwh_per_1m_tokens"] = kwh_per_1m
        sysfs_raw["eur_per_1m_tokens"] = eur_per_1m
        sysfs_raw["electricity_eur_per_kwh"] = round(eur_per_kwh, 2)
        sysfs_raw["discrete_ref_tok_per_j"] = round(discrete_ref, 2)
        sysfs_raw["vs_discrete_ratio"] = vs_discrete
        sysfs_raw["history_efficiency"] = list(self._history_efficiency)

        # Répartition mémoire APU unifiée (total réel RAM + VRAM carve-out)
        vram_used = float(sysfs_raw.get("vram_used_gb", 0.0))
        vram_total = float(sysfs_raw.get("vram_total_gb", 0.0))
        gtt_used = float(sysfs_raw.get("gtt_used_gb", 0.0))
        mem_total_ram = float(meta.get("mem_total_gb", 0.0))
        mem_avail_ram = float(meta.get("mem_available_gb", 0.0))

        apu_total_gb = round(vram_total + mem_total_ram, 1) or 64.0
        rocm_gb = float(vllm_data.get("gtt_pool_gb", 0.0)) if vllm_data.get("online") else 0.0

        if vulkan_data.get("uma_alloc_gb") is not None:
            vulkan_gb = float(vulkan_data["uma_alloc_gb"])
        elif vulkan_data.get("online"):
            vulkan_gb = round(max(0.0, (vram_used + gtt_used) - rocm_gb), 1)
        else:
            vulkan_gb = 0.0

        os_used_ram = max(0.0, mem_total_ram - mem_avail_ram)
        # Isoler la part OS pure hors allocation IA déjà comptabilisée en GTT
        os_gb = round(max(0.0, os_used_ram - max(0.0, vulkan_gb + rocm_gb - vram_used)), 1)
        total_ai_gb = round(vulkan_gb + rocm_gb, 1)
        free_gb = round(max(0.0, apu_total_gb - total_ai_gb - os_gb), 1)

        def pct_of_apu(val: float) -> float:
            return round((val / apu_total_gb) * 100.0, 1) if apu_total_gb > 0 else 0.0

        vulkan_pct = pct_of_apu(vulkan_gb)
        rocm_pct = pct_of_apu(rocm_gb)
        os_pct = pct_of_apu(os_gb)
        free_pct = pct_of_apu(free_gb)
        total_ai_pct = pct_of_apu(total_ai_gb)

        # Injection des empreintes calculées dans les cartes moteurs
        vulkan_card = {
            "online": bool(vulkan_data.get("online", False)),
            "badge": vulkan_data["badge"],
            "model": vulkan_data["model"],
            "host": vulkan_data["host"],
            "port": vulkan_data["port"],
            "status_label": vulkan_data["status_label"],
            "slots": vulkan_data.get("slots") or [],
            "slots_active": int(vulkan_data.get("slots_active", 0)),
            "slots_total": int(vulkan_data.get("slots_total", 0)),
            "speed_tps": vulkan_data["speed_tps"],
            "uma_alloc_gb": round(vulkan_gb, 1),
            "uma_alloc_pct": vulkan_pct,
            "kv_alloc_gb": vulkan_data["kv_alloc_gb"],
            "ctx_label": vulkan_data["ctx_label"],
            "kv_used_tokens": vulkan_data["kv_used_tokens"],
            "kv_max_tokens": vulkan_data["kv_max_tokens"],
            "kv_pct": vulkan_data["kv_pct"],
            "flags": vulkan_data["flags"],
            "client_name": vulkan_data["client_name"],
            "proxy_name": vulkan_data["proxy_name"],
            "proxy_port": vulkan_data["proxy_port"],
        }

        ow_reqs = sqlite_data.get("openwebui", {}).get("requests") or []
        rocm_card = {
            "online": bool(vllm_data.get("online", False)),
            "badge": vllm_data["badge"],
            "model": vllm_data["model"],
            "host": vllm_data["host"],
            "port": vllm_data["port"],
            "running_reqs": vllm_data["running_reqs"],
            "waiting_reqs": vllm_data["waiting_reqs"],
            "speed_tps": vllm_data["speed_tps"],
            "gtt_pool_gb": round(rocm_gb, 1),
            "gtt_pool_pct": rocm_pct,
            "gtt_active_gb": vllm_data["gtt_active_gb"],
            "paged_cache_pct": vllm_data["paged_cache_pct"],
            "prefix_cache_hit_pct": vllm_data["prefix_cache_hit_pct"],
            "preemptions": vllm_data["preemptions"],
            "avg_ttft_sec": vllm_data["avg_ttft_sec"],
            "flags": [*vllm_data["flags"], meta.get("gfx_arch", "gfx1151")],
            "client_name": vllm_data["client_name"],
            "client_port": vllm_data["client_port"],
            "vllm_requests": vllm_data["vllm_requests"] or (ow_reqs if vllm_data["online"] else []),
        }

        # Context DNA & Session Hero : STRICTEMENT verrouillés sur le dernier / actif LLM LOCAL
        # (Ne jamais injecter de modèle/compteurs Cloud Gemini dans le cadre Inférence Locale)
        oc_sess = sqlite_data.get("opencode", {}).get("latest_session") or {}
        hm_sess = sqlite_data.get("hermes", {}).get("latest_session") or {}
        hm_local_sess = sqlite_data.get("hermes", {}).get("latest_local_session") or {}
        local_windows = sqlite_data.get("local_time_windows") or {"SESSION": 0, "1H": 0, "24H": 0, "7J": 0, "TOTAL": 0}

        if is_rocm:
            local_backend = "rocm"
            local_port = int(rocm_card["port"])
            local_status = f"BATCH ({vllm_data.get('running_reqs', 0)} req)"
            first_req = ow_reqs[0] if ow_reqs else {}
            active_title = first_req.get("prompt") or "Session vLLM ROCm / OpenWebUI"
            workspace_dir = str(Path(__file__).resolve().parent.parent)
            tools_cnt = int(vllm_data.get("running_reqs", 0))
            agent_label = "vLLM BATCH"
            active_model = vllm_data.get("model", "vLLM Model")
            active_quant = "(ROCm 7.x · PagedAttention)"
            tokens_session = local_windows.get("SESSION", 0) or int(vulkan_data.get("local_served_tokens", 0))
            prompt_tot = 0
            out_tok = 0
            rsn_tok = 0
            kv_cached = 0
            ctx_used = 0
            ctx_max = 65536
            ctx_pct = int(vllm_data.get("paged_cache_pct", 0))
            ctx_free = ctx_max
            ttft_source = "live"
            hero_flags = rocm_card.get("flags", [])
        else:
            local_backend = "vulkan"
            local_port = int(vulkan_card["port"])
            local_status = str(vulkan_data.get("status_label") or "OFFLINE")
            oc_ts_val = float(oc_sess.get("updated_at") or 0.0)
            hm_loc_ts_val = float(hm_local_sess.get("updated_at") or 0.0)
            local_sess = hm_local_sess if (hm_loc_ts_val > oc_ts_val and hm_local_sess) else oc_sess

            active_title = local_sess.get("title") or "Session Locale (Prête)"
            workspace_dir = (
                local_sess.get("directory")
                or local_sess.get("cwd")
                or str(Path(__file__).resolve().parent.parent)
            )
            tools_cnt = int(local_sess.get("tool_calls", 0))
            agent_label = str(local_sess.get("agent") or "BUILD").upper()
            active_model = vulkan_data["model"] if vulkan_data.get("online") else local_sess.get("model", "Aucun modèle local")
            q_label = vulkan_data.get("quant") or "GGUF"
            active_quant = f"({q_label} · {vulkan_data['kv_max_tokens']:,} ctx · {local_status})".replace(",", " ")
            tokens_session = local_windows.get("SESSION", 0) or int(vulkan_data.get("local_served_tokens", 0))
            prompt_tot = int(local_sess.get("tokens_in", 0) or vulkan_data.get("local_prompt_tokens", 0))
            out_tok = int(local_sess.get("tokens_out", 0) or vulkan_data.get("local_generated_tokens", 0))
            rsn_tok = int(local_sess.get("tokens_rsn", 0))
            kv_cached = int(local_sess.get("tokens_cache_read", 0) or vulkan_data.get("kv_cached_tokens", 0))
            ctx_used = int(vulkan_data.get("kv_used_tokens", 0))
            ctx_max = int(vulkan_data.get("kv_max_tokens", 65536))
            ctx_pct = int(vulkan_data.get("kv_pct", 0))
            ctx_free = max(0, ctx_max - ctx_used)
            ttft_source = str(vulkan_data.get("ttft_source") or "idle")
            hero_flags = vulkan_data.get("tuning_badges", [])

        kv_denom = prompt_tot + kv_cached
        kv_hit_pct = round((kv_cached / kv_denom) * 100.0, 1) if kv_denom > 0 else 0.0
        rsn_ratio_pct = round((rsn_tok / max(1, out_tok + rsn_tok)) * 100) if (out_tok + rsn_tok) > 0 else 0
        loop_status = "WARN" if rsn_ratio_pct > 75 else "OK"

        reuse_flag = next((f for f in vulkan_data.get("flags", []) if f.startswith("Reuse ")), "")
        reuse_txt = f"--cache-reuse {reuse_flag.split()[-1]} actif" if reuse_flag else "Prefill local"
        if ttft_source == "est":
            ttft_sub = f"{reuse_txt} · est. batch"
        elif ttft_source == "last_run":
            ttft_sub = f"{reuse_txt} · dernier run"
        else:
            ttft_sub = reuse_txt

        extra_instances = list(vulkan_data.get("extra_lemonade") or []) + list(ollama_instances or [])
        extra_gb = sum(float(x.get("uma_alloc_gb") or 0.0) for x in extra_instances)
        if extra_gb > 0:
            total_ai_gb = round(total_ai_gb + extra_gb, 1)
            free_gb = round(max(0.0, apu_total_gb - total_ai_gb - os_gb), 1)
            free_pct = pct_of_apu(free_gb)
            total_ai_pct = pct_of_apu(total_ai_gb)

        hm_cloud_sess = sqlite_data.get("hermes", {}).get("latest_cloud_session") or hm_sess
        provider_name = str(hm_cloud_sess.get("provider") or "gemini").upper()
        cloud_model_name = str(hm_cloud_sess.get("model") or "gemini-4-hydrogen")

        active_chains = self._build_active_chains(
            services=services,
            sqlite_data=sqlite_data,
            vulkan_card=vulkan_card,
            rocm_card=rocm_card,
            extra_instances=extra_instances,
            provider_name=provider_name,
            cloud_model_name=cloud_model_name,
        )
        if active_chains:
            active_topology = int(active_chains[0].get("topo_num") or active_topology)

        return {
            "schema_version": "4.2.4",
            "timestamp": round(time.time(), 3),
            "active_topology": active_topology,
            "active_chains": active_chains,
            "timeframe": "SESSION",
            "service_ports": {
                "proxy": int(services["proxy"]["port"]),
                "vulkan": int(services["vulkan"]["port"]),
                "vllm": int(services["vllm_rocm"]["port"]),
                "webui": int(services["open_webui"]["port"]),
                "lemonade_admin": int(services["lemonade_admin"]["port"]),
                "ollama": int(services.get("ollama", {}).get("port", 11434)),
            },
            "runtime_config": {
                "cpu_model": meta.get("cpu_model", "AMD Ryzen AI"),
                "gpu_model": meta.get("gpu_model", "Radeon GPU"),
                "gfx_arch": meta.get("gfx_arch", "gfx115x"),
                "backend": "rocm" if is_rocm else "vulkan",
                "runtime": "vllm" if is_rocm else "llama",
                "port": int(rocm_card["port"] if is_rocm else vulkan_card["port"]),
                "provider_name": provider_name,
                "provider_tier": "API",
                "model_name": cloud_model_name,
                "hermes_profile": sqlite_data["cloud_quotas"].get("active_profile", "default"),
                "include_all_profiles": bool(sqlite_data["cloud_quotas"].get("include_all_profiles", False)),
                "config_file": self._config_file_name,
                "idle_baseline_watts": sysfs_raw.get("idle_baseline_watts", 12.0),
                "idle_baseline_source": sysfs_raw.get("idle_baseline_source", "fallback"),
                "platform_profile": sysfs_raw.get("platform_profile", "default"),
            },
            "session_hero": {
                "local_backend": local_backend,
                "local_port": local_port,
                "local_status": local_status,
                "active_model_title": active_model,
                "active_model_quant": active_quant,
                "active_task_title": active_title,
                "agent_mode": f"[{agent_label} MODE · {tools_cnt} tools]",
                "workspace_dir": workspace_dir,
                "tokens_total_session": tokens_session,
                "speed_tg_tps": tg_speed,
                "speed_pp_tps": pp_speed,
                "ttft_sec": ttft_sec,
                "ttft_source": ttft_source,
                "ttft_sub": ttft_sub,
                "tuning_flags": hero_flags,
                "local_model_config": vllm_data.get("model_config") if is_rocm else vulkan_data.get("model_config"),
            },
            "context_dna": {
                "ctx_used": ctx_used,
                "ctx_max": ctx_max,
                "ctx_pct": ctx_pct,
                "ctx_free": ctx_free,
                "kv_cached_tokens": kv_cached,
                "kv_hit_pct": kv_hit_pct,
                "prompt_in_delta": min(prompt_tot, ctx_used),
                "prompt_in_total": prompt_tot,
                "completion_out_tokens": out_tok,
                "reasoning_tokens": rsn_tok,
                "reasoning_ratio_pct": rsn_ratio_pct,
                "loop_detector_status": loop_status,
                "stop_tokens_active": True,
            },
            "apu_pool_128g": {
                "total_gb": apu_total_gb,
                "vulkan_gb": round(vulkan_gb, 1),
                "vulkan_pct": vulkan_pct,
                "rocm_gb": round(rocm_gb, 1),
                "rocm_pct": rocm_pct,
                "os_gb": round(os_gb, 1),
                "os_pct": os_pct,
                "free_gb": round(free_gb, 1),
                "free_pct": free_pct,
                "total_ai_gb": round(total_ai_gb, 1),
                "total_ai_pct": total_ai_pct,
                "local_generated_tokens": vulkan_data.get("local_generated_tokens", 0),
                "local_served_tokens": vulkan_data.get("local_served_tokens", 0),
            },
            "inference_engines": {
                "vulkan": vulkan_card,
                "rocm": rocm_card,
                "extra_instances": extra_instances,
            },
            "hardware_amd": sysfs_raw,
            "cloud_quotas": sqlite_data["cloud_quotas"],
            "local_sessions": sqlite_data.get("local_sessions") or [],
            "local_cumul_windows": sqlite_data.get("local_cumul_windows")
            or {"1H": 0, "4H": 0, "24H": 0, "sessions_1H": 0, "sessions_4H": 0, "sessions_24H": 0},
            "time_windows": sqlite_data["time_windows"],
            "local_time_windows": local_windows,
        }

    def _build_active_chains(
        self,
        services: Dict[str, Any],
        sqlite_data: Dict[str, Any],
        vulkan_card: Dict[str, Any],
        rocm_card: Dict[str, Any],
        extra_instances: List[Dict[str, Any]],
        provider_name: str,
        cloud_model_name: str,
    ) -> List[Dict[str, Any]]:
        """Construire dynamiquement toutes les chaînes d'exécution actives ou chaudes en parallèle."""
        chains: List[Dict[str, Any]] = []
        proxy_port = int(services["proxy"]["port"])
        vk_port = int(services["vulkan"]["port"])
        vllm_port = int(services["vllm_rocm"]["port"])
        webui_port = int(services["open_webui"]["port"])

        # 1. Filière Cloud API (Hermes multi-profils / OpenCode Cloud)
        cq = sqlite_data.get("cloud_quotas") or {}
        c_sessions = list(cq.get("sessions") or [])
        active_c = [s for s in c_sessions if s.get("status") == "ACTIVE"]
        warm_c = [s for s in c_sessions if s.get("status") == "OPEN" and float(s.get("age_sec") or 999999) <= 7200]
        recent_c = [s for s in c_sessions if float(s.get("age_sec") or 999999) <= 1800]
        tpm_60s = int(cq.get("tpm_sliding_60s") or 0)
        tpm_ceil = max(1, int(cq.get("tpm_ceiling") or 2_000_000))
        tpm_pct = int(cq.get("tpm_pct") or 0)
        cloud_live = bool(active_c or tpm_60s > 0)
        cloud_ready = bool(not cloud_live and (warm_c or recent_c))

        if cloud_live or cloud_ready:
            rel_c = list(active_c) + [s for s in warm_c if s not in active_c]
            if not rel_c:
                rel_c = recent_c or c_sessions[:1]
            hermes_profs: List[str] = []
            models_c: List[str] = []
            has_oc_cloud = False
            for s in rel_c:
                if s.get("source") == "opencode":
                    has_oc_cloud = True
                else:
                    p_lbl = str(s.get("profile") or "default")
                    if p_lbl not in hermes_profs:
                        hermes_profs.append(p_lbl)
                m_lbl = str(s.get("model") or "")
                if m_lbl and m_lbl not in models_c:
                    models_c.append(m_lbl)

            if hermes_profs and has_oc_cloud:
                c_client_name = "HERMES + OPENCODE"
                c_client_sub = f"Profils: {' · '.join(hermes_profs[:3])}"
                c_client_icon = "fa-terminal"
            elif has_oc_cloud:
                c_client_name = "OPENCODE CLI"
                c_client_sub = "Session Cloud Directe"
                c_client_icon = "fa-code"
            else:
                c_client_name = "HERMES CLI"
                p_prefix = "Profils" if len(hermes_profs) > 1 else "Profil"
                c_client_sub = f"{p_prefix}: {' · '.join(hermes_profs[:3]) or 'default'}"
                c_client_icon = "fa-terminal"

            tot_tools_c = sum(int(s.get("tool_calls") or 0) for s in rel_c)
            c_client_meta = f"{tot_tools_c} tools · {len(rel_c)} session{'s' if len(rel_c) > 1 else ''}"
            prov_raw = str((rel_c[0].get("provider") if rel_c else "") or provider_name or "CLOUD")
            prov_clean = prov_raw.split(":")[0].split("/")[0].upper()
            mod_sub = (
                models_c[0]
                if len(models_c) == 1
                else (f"{models_c[0]} +{len(models_c) - 1}" if models_c else cloud_model_name)
            )
            c_state = "LIVE" if cloud_live else "READY"
            chains.append(
                {
                    "id": "cloud_api",
                    "topo_num": 4,
                    "state": c_state,
                    "short_label": f"CLOUD {prov_clean}",
                    "label": f"{c_client_name} ({'·'.join(hermes_profs[:2]) or 'cloud'}) ──► {prov_clean} API ({mod_sub})",
                    "nodes": [
                        {
                            "key": "hermes_cloud",
                            "icon": c_client_icon,
                            "color": "cyan",
                            "name": c_client_name,
                            "sub": c_client_sub,
                            "meta": c_client_meta,
                            "status": "ACTIF" if cloud_live else "OUVERT",
                            "live": cloud_live,
                        },
                        {
                            "key": "cloud",
                            "icon": "fa-cloud",
                            "color": "amber",
                            "name": f"{prov_clean} (API)",
                            "sub": mod_sub,
                            "meta": f"TPM {tpm_60s / 1e6:.2f}M / {tpm_ceil / 1e6:.1f}M ({tpm_pct}%)",
                            "status": "CLOUD · RUN" if cloud_live else "CLOUD · READY",
                            "live": cloud_live,
                        },
                    ],
                }
            )

        # 2. Filière Locale Vulkan / Lemonade (:13306 -> :8001)
        vk_online = bool(vulkan_card.get("online", False))
        vk_status = str(vulkan_card.get("status_label") or "OFFLINE")
        vk_running = vk_online and ("RUN" in vk_status.upper())
        l_sessions = list(sqlite_data.get("local_sessions") or [])
        active_l = [s for s in l_sessions if s.get("status") == "ACTIVE"]
        warm_l = [s for s in l_sessions if s.get("status") == "OPEN" and float(s.get("age_sec") or 999999) <= 3600]
        vk_live = bool(vk_running or active_l)
        vk_ready = bool(not vk_live and (vk_online or warm_l))

        if vk_live or vk_ready:
            recent_l = active_l or warm_l or [s for s in l_sessions if float(s.get("age_sec") or 999999) <= 3600] or l_sessions[:1]
            hm_loc = [s for s in recent_l if s.get("source") == "hermes"]
            oc_loc = [s for s in recent_l if s.get("source") == "opencode"]
            vk_nodes: List[Dict[str, Any]] = []
            vk_topo = 1
            chain_head_lbl = "CLIENT LOCAL"

            if hm_loc and oc_loc:
                profs_l = list(dict.fromkeys(str(s.get("profile") or "default") for s in hm_loc))
                tools_hm = sum(int(s.get("tool_calls") or 0) for s in hm_loc)
                tools_oc = sum(int(s.get("tool_calls") or 0) for s in oc_loc)
                chain_head_lbl = f"HERMES ({'·'.join(profs_l[:2])}) ──► OPENCODE"
                vk_nodes.append(
                    {
                        "key": "hermes_local",
                        "icon": "fa-terminal",
                        "color": "cyan",
                        "name": "HERMES CLI",
                        "sub": f"Profil: {' · '.join(profs_l[:2])}",
                        "meta": f"{tools_hm} tools · Orchestrateur",
                        "status": "ACTIF" if vk_live else "PRÊT",
                        "live": vk_live,
                    }
                )
                vk_nodes.append(
                    {
                        "key": "opencode_local",
                        "icon": "fa-code",
                        "color": "purple",
                        "name": "OPENCODE",
                        "sub": "Agent Build / Code",
                        "meta": f"{tools_oc} tools · Local",
                        "status": "LOCAL" if vk_live else "IDLE",
                        "live": vk_live,
                    }
                )
            elif hm_loc:
                profs_l = list(dict.fromkeys(str(s.get("profile") or "default") for s in hm_loc))
                tools_hm = sum(int(s.get("tool_calls") or 0) for s in hm_loc)
                chain_head_lbl = f"HERMES ({'·'.join(profs_l[:2])})"
                vk_nodes.append(
                    {
                        "key": "hermes_local",
                        "icon": "fa-terminal",
                        "color": "cyan",
                        "name": "HERMES CLI",
                        "sub": f"Profil: {' · '.join(profs_l[:2])}",
                        "meta": f"{tools_hm} tools · Session Locale",
                        "status": "ACTIF" if vk_live else "OUVERT",
                        "live": vk_live,
                    }
                )
            elif oc_loc:
                vk_topo = 2
                tools_oc = sum(int(s.get("tool_calls") or 0) for s in oc_loc)
                chain_head_lbl = "OPENCODE CLI"
                vk_nodes.append(
                    {
                        "key": "opencode_direct",
                        "icon": "fa-code",
                        "color": "purple",
                        "name": "OPENCODE CLI",
                        "sub": "Session TUI Autonome",
                        "meta": f"{tools_oc} tools · Direct",
                        "status": "ACTIF" if vk_live else "IDLE",
                        "live": vk_live,
                    }
                )
            else:
                chain_head_lbl = "ENDPOINT LOCAL"
                vk_nodes.append(
                    {
                        "key": "local_client",
                        "icon": "fa-terminal",
                        "color": "cyan",
                        "name": "CLIENT LOCAL",
                        "sub": "OpenAI-compat API",
                        "meta": "Moteur chargé en mémoire",
                        "status": "VEILLE",
                        "live": False,
                    }
                )

            vk_kv_pct = int(vulkan_card.get("kv_pct") or 0)
            vk_spd = float(vulkan_card.get("speed_tps") or 0.0)
            vk_mod = str(vulkan_card.get("model") or "Modèle Vulkan")
            vk_nodes.append(
                {
                    "key": "proxy",
                    "icon": "fa-arrows-left-right-to-line",
                    "color": "amber",
                    "name": f"PROXY :{proxy_port}",
                    "sub": "Lemonade Router",
                    "meta": "SSE Stream · OpenAI v1",
                    "status": "STREAM" if vk_live else "READY",
                    "live": vk_live,
                }
            )
            vk_nodes.append(
                {
                    "key": "vulkan",
                    "icon": "fa-microchip",
                    "color": "emerald",
                    "name": f"VULKAN :{vk_port}",
                    "sub": vk_mod,
                    "meta": f"KV {vk_kv_pct}% · {vk_spd:.1f} t/s",
                    "status": vk_status if vk_online else "OFFLINE",
                    "live": vk_live,
                }
            )
            chains.append(
                {
                    "id": "local_vulkan",
                    "topo_num": vk_topo,
                    "state": "LIVE" if vk_live else "READY",
                    "short_label": f"VULKAN :{vk_port}",
                    "label": f"{chain_head_lbl} ──► PROXY :{proxy_port} ──► VULKAN :{vk_port} ({vk_mod})",
                    "nodes": vk_nodes,
                }
            )

        # 3. Filière Locale ROCm / vLLM (:8080 -> :8000)
        rc_online = bool(rocm_card.get("online", False))
        rc_reqs = int(rocm_card.get("running_reqs") or 0)
        rc_live = bool(rc_online and rc_reqs > 0)
        rc_ready = bool(rc_online and not rc_live)

        if rc_live or rc_ready:
            rc_paged = int(rocm_card.get("paged_cache_pct") or 0)
            rc_spd = float(rocm_card.get("speed_tps") or 0.0)
            rc_mod = str(rocm_card.get("model") or "vLLM ROCm")
            chains.append(
                {
                    "id": "local_rocm",
                    "topo_num": 3,
                    "state": "LIVE" if rc_live else "READY",
                    "short_label": f"ROCM :{vllm_port}",
                    "label": f"OPENWEBUI :{webui_port} ──► VLLM ROCM :{vllm_port} ({rc_mod})",
                    "nodes": [
                        {
                            "key": "webui",
                            "icon": "fa-globe",
                            "color": "emerald",
                            "name": f"OPEN WEBUI :{webui_port}",
                            "sub": "Portail Multi-Users",
                            "meta": f"{len(rocm_card.get('vllm_requests') or [])} session(s) Web",
                            "status": f"{rc_reqs} REQ" if rc_live else "READY",
                            "live": rc_live,
                        },
                        {
                            "key": "vllm",
                            "icon": "fa-layer-group",
                            "color": "rose",
                            "name": f"VLLM ROCM :{vllm_port}",
                            "sub": rc_mod,
                            "meta": f"Paged {rc_paged}% · {rc_spd:.1f} t/s",
                            "status": f"BATCH ({rc_reqs} req)" if rc_live else "IDLE",
                            "live": rc_live,
                        },
                    ],
                }
            )

        # 4. Filières Moteurs Additionnels (Ollama / instances Lemonade supplémentaires chargées)
        for inst in extra_instances:
            if not inst.get("online", True):
                continue
            alloc_gb = float(inst.get("uma_alloc_gb") or 0.0)
            st_raw = str(inst.get("status") or "IDLE").upper()
            e_live = "RUN" in st_raw or "ACTIVE" in st_raw
            if alloc_gb <= 0.0 and not e_live:
                continue
            e_port = inst.get("port") or 11434
            e_eng = str(inst.get("engine") or "ENGINE").upper()
            e_mod = str(inst.get("model") or "Modèle Local")
            chains.append(
                {
                    "id": f"extra_{e_port}",
                    "topo_num": 2,
                    "state": "LIVE" if e_live else "READY",
                    "short_label": f"{e_eng} :{e_port}",
                    "label": f"CLIENT LOCAL ──► {e_eng} :{e_port} ({e_mod})",
                    "nodes": [
                        {
                            "key": f"client_{e_port}",
                            "icon": "fa-terminal",
                            "color": "purple",
                            "name": "CLIENT LOCAL",
                            "sub": "API Locale Directe",
                            "meta": "Requête HTTP locale",
                            "status": "ACTIF" if e_live else "PRÊT",
                            "live": e_live,
                        },
                        {
                            "key": f"extra_{e_port}",
                            "icon": "fa-server",
                            "color": "cyan",
                            "name": f"{e_eng} :{e_port}",
                            "sub": e_mod,
                            "meta": f"UMA {alloc_gb:.1f} Go",
                            "status": st_raw,
                            "live": e_live,
                        },
                    ],
                }
            )

        # Trier : chaînes LIVE en tête, puis READY
        chains.sort(key=lambda c: 0 if c.get("state") == "LIVE" else 1)
        return chains

    def write_ipc_atomic(self, payload: Dict[str, Any]) -> None:
        """Écrire via fichier temporaire + os.replace vers /tmp/tokenwatcher_widget_state.json."""
        ipc_cfg = self._full_config.get("ipc", DEFAULT_PORTS_CONFIG["ipc"])
        target_path = Path(str(ipc_cfg.get("state_file", "/tmp/tokenwatcher_widget_state.json")))
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")

        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)

    async def run_forever(self) -> None:
        """Boucle principale selon ipc.polling_interval_sec."""
        self.sysfs_probe.discover_sensors()
        cfg = self.load_ports_config()
        interval = float(cfg.get("ipc", {}).get("polling_interval_sec", 1.0))

        while True:
            t0 = time.monotonic()
            try:
                payload = await self.collect_once()
                self.write_ipc_atomic(payload)
            except Exception as exc:
                print(f"Erreur boucle collecte TokenWatcher: {exc}")
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.1, interval - elapsed))


async def _run_once(daemon: TokenWatcherDaemon) -> None:
    daemon.sysfs_probe.discover_sensors()
    daemon.load_ports_config()
    payload = await daemon.collect_once()
    daemon.write_ipc_atomic(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TokenWatcher Telemetry Daemon V4.2.4")
    parser.add_argument("--once", action="store_true", help="Exécute un cycle de collecte unique puis quitte.")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    daemon = TokenWatcherDaemon(config_dir=root_dir / "config")
    if args.once:
        asyncio.run(_run_once(daemon))
    else:
        asyncio.run(daemon.run_forever())
