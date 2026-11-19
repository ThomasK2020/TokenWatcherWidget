#!/usr/bin/env python3
"""
Démon Principal TokenWatcher V4.0.2 (SOW Lot 1).
Orchestre les 3 sondes (AmdSysfsProbe, EnginesHttpProbe, SqliteTelemetryReader)
à un intervalle de 1.0s et écrit atomiquement l'état dans /tmp/tokenwatcher_state.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict

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
    },
    "ipc": {
        "state_file": "/tmp/tokenwatcher_state.json",
        "polling_interval_sec": 1.0,
    },
}


class TokenWatcherDaemon:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.sysfs_probe = AmdSysfsProbe()
        self.sqlite_reader = SqliteTelemetryReader()
        self.engines_probe: EnginesHttpProbe | None = None
        self._full_config: Dict[str, Any] = {}

    def load_ports_config(self) -> Dict[str, Any]:
        """Charger config/ports.yaml avec fallback mémoire sécurisé."""
        merged = {
            "services": {k: dict(v) for k, v in DEFAULT_PORTS_CONFIG["services"].items()},
            "ipc": dict(DEFAULT_PORTS_CONFIG["ipc"]),
        }
        ports_file = self.config_dir / "ports.yaml"
        if ports_file.is_file():
            try:
                loaded = yaml.safe_load(ports_file.read_text(encoding="utf-8")) or {}
                if isinstance(loaded.get("services"), dict):
                    for k, v in loaded["services"].items():
                        if isinstance(v, dict):
                            merged["services"].setdefault(k, {}).update(v)
                if isinstance(loaded.get("ipc"), dict):
                    merged["ipc"].update(loaded["ipc"])
            except Exception as exc:
                print(f"Avertissement lecture ports.yaml: {exc}")

        self._full_config = merged
        self.engines_probe = EnginesHttpProbe(merged)
        return merged

    async def collect_once(self) -> Dict[str, Any]:
        """Fusionner les données des 3 sondes dans le schéma JSON V4.0.2."""
        if not self._full_config or self.engines_probe is None:
            self.load_ports_config()

        assert self.engines_probe is not None

        sysfs_raw, sqlite_data, vulkan_data, vllm_data = await asyncio.gather(
            asyncio.to_thread(self.sysfs_probe.read_metrics),
            asyncio.to_thread(self.sqlite_reader.read_active_topology_and_tokens),
            self.engines_probe.poll_vulkan_slots(),
            self.engines_probe.poll_vllm_metrics(),
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

        # Efficacité énergétique réelle tok/J
        gfx_w = float(sysfs_raw.get("gfx_power_w", 0.0))
        sysfs_raw["efficiency_tok_per_j"] = round(tg_speed / gfx_w, 2) if (gfx_w > 0 and tg_speed > 0) else 0.0

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
            "badge": vulkan_data["badge"],
            "model": vulkan_data["model"],
            "host": vulkan_data["host"],
            "port": vulkan_data["port"],
            "status_label": vulkan_data["status_label"],
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

        # Context DNA & Session Hero (arbitrage live SQLite + /slots)
        oc_sess = sqlite_data.get("opencode", {}).get("latest_session") or {}
        hm_sess = sqlite_data.get("hermes", {}).get("latest_session") or {}

        if active_topology == 4 and hm_sess:
            active_title = hm_sess.get("title", "Hermes Session")
            workspace_dir = hm_sess.get("cwd") or str(Path(__file__).resolve().parent.parent)
            tools_cnt = hm_sess.get("tool_calls", 0)
            active_model = hm_sess.get("model", "Cloud Model")
            active_quant = "(Cloud Frontier)"
            tokens_session = hm_sess.get("tokens_total", 0)
            prompt_tot = hm_sess.get("tokens_in", 0)
            out_tok = hm_sess.get("tokens_out", 0)
            rsn_tok = hm_sess.get("tokens_rsn", 0)
            kv_cached = hm_sess.get("tokens_cache_read", 0)
        else:
            active_title = oc_sess.get("title") or hm_sess.get("title") or "Session Active"
            workspace_dir = oc_sess.get("directory") or hm_sess.get("cwd") or str(Path(__file__).resolve().parent.parent)
            tools_cnt = hm_sess.get("tool_calls", 0)
            active_model = vulkan_data["model"] if vulkan_data.get("online") else oc_sess.get("model", "Aucun modèle")
            q_label = vulkan_data.get("quant") or "GGUF"
            active_quant = f"({q_label} · {vulkan_data['kv_max_tokens']:,} ctx)".replace(",", " ")
            tokens_session = sqlite_data.get("time_windows", {}).get("SESSION", 0) or vulkan_data.get("local_served_tokens", 0)
            prompt_tot = oc_sess.get("tokens_in", 0) or vulkan_data.get("local_prompt_tokens", 0)
            out_tok = oc_sess.get("tokens_out", 0) or vulkan_data.get("local_generated_tokens", 0)
            rsn_tok = oc_sess.get("tokens_rsn", 0)
            kv_cached = vulkan_data.get("kv_cached_tokens", 0) or oc_sess.get("tokens_cache_read", 0)

        ctx_used = vulkan_data.get("kv_used_tokens", 0)
        ctx_max = vulkan_data.get("kv_max_tokens", 65536)
        ctx_pct = vulkan_data.get("kv_pct", 0)
        ctx_free = max(0, ctx_max - ctx_used)
        kv_denom = prompt_tot + kv_cached
        kv_hit_pct = round((kv_cached / kv_denom) * 100.0, 1) if kv_denom > 0 else 0.0
        rsn_ratio_pct = round((rsn_tok / max(1, out_tok + rsn_tok)) * 100) if (out_tok + rsn_tok) > 0 else 0
        loop_status = "WARN" if rsn_ratio_pct > 75 else "OK"

        provider_name = str(hm_sess.get("provider") or "gemini").upper()
        cloud_model_name = str(hm_sess.get("model") or "gemini-4-hydrogen")

        return {
            "schema_version": "4.0.2",
            "timestamp": round(time.time(), 3),
            "active_topology": active_topology,
            "timeframe": "SESSION",
            "service_ports": {
                "proxy": int(services["proxy"]["port"]),
                "vulkan": int(services["vulkan"]["port"]),
                "vllm": int(services["vllm_rocm"]["port"]),
                "webui": int(services["open_webui"]["port"]),
                "lemonade_admin": int(services["lemonade_admin"]["port"]),
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
            },
            "session_hero": {
                "active_model_title": active_model,
                "active_model_quant": active_quant,
                "active_task_title": active_title,
                "agent_mode": f"[BUILD MODE · {tools_cnt} tools]",
                "workspace_dir": workspace_dir,
                "tokens_total_session": tokens_session,
                "speed_tg_tps": tg_speed,
                "speed_pp_tps": pp_speed,
                "ttft_sec": ttft_sec,
                "tuning_flags": vulkan_data.get("tuning_badges", []),
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
            },
            "hardware_amd": sysfs_raw,
            "cloud_quotas": sqlite_data["cloud_quotas"],
            "time_windows": sqlite_data["time_windows"],
        }

    def write_ipc_atomic(self, payload: Dict[str, Any]) -> None:
        """Écrire via fichier temporaire + os.replace vers /tmp/tokenwatcher_state.json."""
        ipc_cfg = self._full_config.get("ipc", DEFAULT_PORTS_CONFIG["ipc"])
        target_path = Path(str(ipc_cfg.get("state_file", "/tmp/tokenwatcher_state.json")))
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
    parser = argparse.ArgumentParser(description="TokenWatcher Telemetry Daemon V4.0.2")
    parser.add_argument("--once", action="store_true", help="Exécute un cycle de collecte unique puis quitte.")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    daemon = TokenWatcherDaemon(config_dir=root_dir / "config")
    if args.once:
        asyncio.run(_run_once(daemon))
    else:
        asyncio.run(daemon.run_forever())
