"""
Sonde HTTP Multi-Moteurs : Lemonade Vulkan (/slots & /metrics) & vLLM ROCm (/metrics).
Utilise EXCLUSIVEMENT la Stdlib Python (urllib.request dans asyncio.to_thread).
Inspecte /proc/<pid>/cmdline et /proc/<pid>/fdinfo/* lorsque les permissions le permettent.
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


class EnginesHttpProbe:
    def __init__(self, ports_config: Dict[str, Any]) -> None:
        self.ports_config = ports_config

    def _services(self) -> Dict[str, Any]:
        if "services" in self.ports_config and isinstance(self.ports_config["services"], dict):
            return self.ports_config["services"]
        return self.ports_config

    @staticmethod
    def _http_get_text(url: str, timeout: float = 1.5) -> Optional[str]:
        req = urllib.request.Request(url, headers={"Accept": "application/json, text/plain"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return resp.read().decode("utf-8", errors="ignore")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        return None

    @classmethod
    def _http_get_json(cls, url: str, timeout: float = 1.5) -> Any:
        raw = cls._http_get_text(url, timeout=timeout)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _parse_prometheus(text: str) -> Dict[str, float]:
        metrics: Dict[str, float] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            key = parts[0].split("{", 1)[0]
            try:
                val = float(parts[-1])
                metrics[key] = metrics.get(key, 0.0) + val
            except ValueError:
                continue
        return metrics

    @staticmethod
    def _find_proc_info(binary_tokens: tuple[str, ...]) -> tuple[Optional[int], List[str]]:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                raw_cmd = (pid_dir / "cmdline").read_bytes()
            except OSError:
                continue
            if not raw_cmd:
                continue
            args = [a.decode("utf-8", errors="ignore") for a in raw_cmd.split(b"\x00") if a]
            if not args:
                continue
            joined = " ".join(args).lower()
            if any(tok in joined for tok in binary_tokens):
                return int(pid_dir.name), args
        return None, []

    @staticmethod
    def _read_fdinfo_drm_gb(pid: Optional[int]) -> Optional[float]:
        if pid is None:
            return None
        fdinfo_dir = Path(f"/proc/{pid}/fdinfo")
        total_kib = 0
        seen_ids: set[str] = set()
        read_any = False
        try:
            entries = list(fdinfo_dir.iterdir())
        except OSError:
            return None
        for fd_file in entries:
            try:
                txt = fd_file.read_text(encoding="utf-8", errors="ignore")
                read_any = True
            except OSError:
                continue
            if "drm-driver:" not in txt:
                continue
            client_id = None
            for line in txt.splitlines():
                if line.startswith("drm-client-id:"):
                    client_id = line.split(":", 1)[1].strip()
                    break
            if client_id and client_id in seen_ids:
                continue
            if client_id:
                seen_ids.add(client_id)
            for line in txt.splitlines():
                m = re.match(r"^drm-memory-(?:vram|gtt):\s*(\d+)\s*(KiB|MiB|B)?", line.strip())
                if m:
                    val = int(m.group(1))
                    unit = m.group(2) or "B"
                    if unit == "MiB":
                        total_kib += val * 1024
                    elif unit == "KiB":
                        total_kib += val
                    else:
                        total_kib += val // 1024
        if not read_any or total_kib == 0:
            return None
        return round(total_kib / (1024 * 1024), 2)

    @staticmethod
    def _extract_cli_flags(args: List[str]) -> tuple[List[str], Optional[int], Optional[int]]:
        flags: List[str] = []
        port_found: Optional[int] = None
        ctx_found: Optional[int] = None
        arg_str = " ".join(args)

        m_port = re.search(r"--port\s+(\d+)", arg_str)
        if m_port:
            port_found = int(m_port.group(1))
        m_ctx = re.search(r"(?:--ctx-size|-c)\s+(\d+)", arg_str)
        if m_ctx:
            ctx_found = int(m_ctx.group(1))

        m_ngl = re.search(r"(?:-ngl|--n-gpu-layers)\s+(\d+)", arg_str)
        flags.append(f"NGL {m_ngl.group(1)}" if m_ngl else "NGL 999")

        m_ctk = re.search(r"(?:-ctk|--cache-type-k)\s+(\S+)", arg_str)
        if m_ctk:
            flags.append(f"KV {m_ctk.group(1)}")

        if "-fa on" in arg_str or "--flash-attn" in arg_str:
            flags.append("FA:ON")

        m_reuse = re.search(r"--cache-reuse\s+(\d+)", arg_str)
        if m_reuse:
            flags.append(f"Reuse {m_reuse.group(1)}")

        m_b = re.search(r"(?:^|\s)-b\s+(\d+)", arg_str)
        m_ub = re.search(r"(?:^|\s)-ub\s+(\d+)", arg_str)
        if m_b and m_ub:
            flags.append(f"B {m_b.group(1)}/{m_ub.group(1)}")

        return flags, port_found, ctx_found

    async def poll_vulkan_slots(self) -> Dict[str, Any]:
        """Interroger Lemonade /api/v1/health puis GET /slots & /metrics sur le port Vulkan."""
        services = self._services()
        v_cfg = services.get("vulkan", {})
        admin_cfg = services.get("lemonade_admin", {})
        proxy_cfg = services.get("proxy", {})

        v_host = v_cfg.get("host", "127.0.0.1")
        v_port = int(v_cfg.get("port", 8001))
        slots_ep = v_cfg.get("slots_endpoint", "/slots")
        metrics_ep = v_cfg.get("metrics_endpoint", "/metrics")

        admin_host = admin_cfg.get("host", "127.0.0.1")
        admin_port = int(admin_cfg.get("port", 13305))
        health_ep = admin_cfg.get("health_endpoint", "/api/v1/health")
        proxy_port = int(proxy_cfg.get("port", 13306))

        health_url = f"http://{admin_host}:{admin_port}{health_ep}"
        health_data = await asyncio.to_thread(self._http_get_json, health_url, 1.0)

        pid, cmd_args = await asyncio.to_thread(self._find_proc_info, ("llama-server",))
        model_name = "Aucun modèle chargé"
        quant_str = ""
        launch_args: List[str] = list(cmd_args)
        ctx_from_health: Optional[int] = None

        if isinstance(health_data, dict):
            loaded_list = health_data.get("all_models_loaded") or []
            if loaded_list and isinstance(loaded_list[0], dict):
                m_info = loaded_list[0]
                model_name = m_info.get("model_name") or health_data.get("model_loaded") or model_name
                if pid is None and isinstance(m_info.get("pid"), int):
                    pid = m_info["pid"]
                if not launch_args and isinstance(m_info.get("launch_command"), list):
                    launch_args = [str(x) for x in m_info["launch_command"]]
                backend_url = m_info.get("backend_url")
                if isinstance(backend_url, str):
                    parsed_u = urllib.parse.urlparse(backend_url)
                    if parsed_u.port:
                        v_port = parsed_u.port
                    if parsed_u.hostname:
                        v_host = parsed_u.hostname
                ckpt = str(m_info.get("checkpoint", ""))
                m_q = re.search(r"(Q\d+_[A-Z0-9_]+|IQ\d+_[A-Z0-9_]+)", ckpt)
                if m_q:
                    quant_str = m_q.group(1)
                ro = m_info.get("recipe_options") or {}
                if isinstance(ro, dict) and ro.get("ctx_size"):
                    ctx_from_health = int(ro["ctx_size"])
            elif health_data.get("model_loaded"):
                model_name = str(health_data["model_loaded"])

        flags, cmd_port, cmd_ctx = self._extract_cli_flags(launch_args)
        if cmd_port and not isinstance(health_data, dict):
            v_port = cmd_port

        slots_url = f"http://{v_host}:{v_port}{slots_ep}"
        metrics_url = f"http://{v_host}:{v_port}{metrics_ep}"
        slots_data, metrics_raw, fdinfo_gb = await asyncio.gather(
            asyncio.to_thread(self._http_get_json, slots_url, 1.5),
            asyncio.to_thread(self._http_get_text, metrics_url, 1.5),
            asyncio.to_thread(self._read_fdinfo_drm_gb, pid),
        )

        prom = self._parse_prometheus(metrics_raw) if metrics_raw else {}
        online = slots_data is not None or bool(prom)

        n_ctx = ctx_from_health or cmd_ctx or 65536
        kv_used = 0
        kv_cached = 0
        is_processing = False
        tg_tps = 0.0
        pp_tps = 0.0
        ttft_sec = 0.0
        temp_param: Optional[float] = None
        rep_pen: Optional[float] = None

        if isinstance(slots_data, list) and slots_data and isinstance(slots_data[0], dict):
            slot0 = slots_data[0]
            n_ctx = int(slot0.get("n_ctx") or n_ctx)
            is_processing = bool(slot0.get("is_processing", False))
            n_past = int(slot0.get("n_past") or slot0.get("n_prompt_tokens") or 0)
            n_decoded = 0
            nxt = slot0.get("next_token")
            if isinstance(nxt, list) and nxt and isinstance(nxt[0], dict):
                n_decoded = int(nxt[0].get("n_decoded") or 0)
            kv_used = n_past + n_decoded
            kv_cached = int(slot0.get("n_prompt_tokens_cache") or 0)
            timings = slot0.get("timings")
            if isinstance(timings, dict):
                tg_tps = float(timings.get("predicted_per_second") or 0.0)
                pp_tps = float(timings.get("prompt_per_second") or 0.0)
                if timings.get("prompt_ms"):
                    ttft_sec = float(timings["prompt_ms"]) / 1000.0
                if timings.get("cache_n"):
                    kv_cached = int(timings["cache_n"])
            params = slot0.get("params")
            if isinstance(params, dict):
                if isinstance(params.get("temperature"), (int, float)):
                    temp_param = round(float(params["temperature"]), 2)
                if isinstance(params.get("repeat_penalty"), (int, float)):
                    rep_pen = round(float(params["repeat_penalty"]), 2)

        # Fallback métriques moyennes Prometheus quand le slot est IDLE
        if tg_tps <= 0.0:
            if prom.get("llamacpp:predicted_tokens_seconds", 0.0) > 0:
                tg_tps = prom["llamacpp:predicted_tokens_seconds"]
            elif prom.get("llamacpp:tokens_predicted_seconds_total", 0.0) > 0:
                tg_tps = prom.get("llamacpp:tokens_predicted_total", 0.0) / prom["llamacpp:tokens_predicted_seconds_total"]
        if pp_tps <= 0.0:
            if prom.get("llamacpp:prompt_tokens_seconds", 0.0) > 0:
                pp_tps = prom["llamacpp:prompt_tokens_seconds"]
            elif prom.get("llamacpp:prompt_seconds_total", 0.0) > 0:
                pp_tps = prom.get("llamacpp:prompt_tokens_total", 0.0) / prom["llamacpp:prompt_seconds_total"]
        if kv_used == 0 and prom.get("llamacpp:n_tokens_max"):
            kv_used = int(prom["llamacpp:n_tokens_max"])

        local_gen_tok = int(prom.get("llamacpp:tokens_predicted_total", 0))
        local_prompt_tok = int(prom.get("llamacpp:prompt_tokens_total", 0))
        local_cached_tok = int(prom.get("llamacpp:prompt_tokens_cached_total", 0))
        local_served_tok = local_gen_tok + local_prompt_tok + local_cached_tok

        kv_pct = min(100, round((kv_used / n_ctx) * 100)) if n_ctx > 0 else 0
        ctx_label = f"{round(n_ctx / 1024)}k" if n_ctx >= 1024 else str(n_ctx)

        if not online:
            status_label = "OFFLINE"
        elif is_processing or prom.get("llamacpp:requests_processing", 0) > 0:
            status_label = "SLOT 0 · RUN"
        else:
            status_label = "SLOT 0 · IDLE"

        tuning_badges: List[str] = []
        if temp_param is not None:
            tuning_badges.append(f"Temp {temp_param}")
        if rep_pen is not None:
            tuning_badges.append(f"RepPen {rep_pen}")
        tuning_badges.extend(flags)
        tuning_badges.append("stream_options: include_usage")

        return {
            "online": online,
            "badge": "[VULKAN]",
            "model": model_name,
            "quant": quant_str,
            "host": f"http://{v_host}",
            "port": v_port,
            "status_label": status_label,
            "speed_tps": round(tg_tps, 1),
            "speed_pp_tps": round(pp_tps),
            "ttft_sec": round(ttft_sec, 2),
            "uma_alloc_gb": fdinfo_gb,
            "kv_alloc_gb": round((kv_used / max(1, n_ctx)) * 2.0, 2) if online else 0.0,
            "ctx_label": ctx_label,
            "kv_used_tokens": kv_used,
            "kv_max_tokens": n_ctx,
            "kv_cached_tokens": kv_cached or local_cached_tok,
            "kv_pct": kv_pct,
            "flags": flags,
            "tuning_badges": tuning_badges,
            "client_name": "OpenCode",
            "proxy_name": "Proxy",
            "proxy_port": proxy_port,
            "local_generated_tokens": local_gen_tok,
            "local_prompt_tokens": local_prompt_tok,
            "local_cached_tokens": local_cached_tok,
            "local_served_tokens": local_served_tok,
        }

    async def poll_vllm_metrics(self) -> Dict[str, Any]:
        """Interroger GET /metrics (Prometheus) sur le port vLLM configuré."""
        services = self._services()
        r_cfg = services.get("vllm_rocm", {})
        w_cfg = services.get("open_webui", {})

        r_host = r_cfg.get("host", "127.0.0.1")
        r_port = int(r_cfg.get("port", 8000))
        metrics_ep = r_cfg.get("metrics_endpoint", "/metrics")
        models_ep = r_cfg.get("models_endpoint", "/v1/models")
        webui_port = int(w_cfg.get("port", 8080))

        metrics_url = f"http://{r_host}:{r_port}{metrics_ep}"
        models_url = f"http://{r_host}:{r_port}{models_ep}"
        pid, cmd_args = await asyncio.to_thread(self._find_proc_info, ("vllm.entrypoints", "vllm serve"))

        metrics_raw, models_data, fdinfo_gb = await asyncio.gather(
            asyncio.to_thread(self._http_get_text, metrics_url, 1.5),
            asyncio.to_thread(self._http_get_json, models_url, 1.0),
            asyncio.to_thread(self._read_fdinfo_drm_gb, pid),
        )

        model_name = "vLLM ROCm (Inactif)"
        if isinstance(models_data, dict):
            d_list = models_data.get("data") or []
            if d_list and isinstance(d_list[0], dict) and d_list[0].get("id"):
                model_name = str(d_list[0]["id"])

        if not metrics_raw:
            return {
                "online": False,
                "badge": "[ROCm 7.x]",
                "model": model_name,
                "host": f"http://{r_host}",
                "port": r_port,
                "running_reqs": 0,
                "waiting_reqs": 0,
                "speed_tps": 0.0,
                "gtt_pool_gb": fdinfo_gb or 0.0,
                "gtt_pool_pct": 0.0,
                "gtt_active_gb": 0.0,
                "paged_cache_pct": 0,
                "prefix_cache_hit_pct": 0,
                "preemptions": 0,
                "avg_ttft_sec": 0.0,
                "flags": ["/dev/kfd", "Continuous Batching"],
                "client_name": "OpenWebUI",
                "client_port": webui_port,
                "vllm_requests": [],
            }

        prom = self._parse_prometheus(metrics_raw)
        running = int(prom.get("vllm:num_requests_running", 0))
        waiting = int(prom.get("vllm:num_requests_waiting", 0))
        kv_factor = prom.get("vllm:gpu_cache_usage_factor", 0.0)
        paged_pct = min(100, max(0, round(kv_factor * 100)))

        hit_queries = prom.get("vllm:gpu_prefix_cache_hit_rate", 0.0)
        if hit_queries == 0.0 and prom.get("vllm:prefix_cache_queries_total", 0.0) > 0:
            hit_queries = prom.get("vllm:prefix_cache_hits_total", 0.0) / prom["vllm:prefix_cache_queries_total"]
        prefix_hit_pct = min(100, max(0, round(hit_queries * 100)))
        preemptions = int(prom.get("vllm:num_preemptions_total", 0))

        ttft_sum = prom.get("vllm:time_to_first_token_seconds_sum", 0.0)
        ttft_cnt = prom.get("vllm:time_to_first_token_seconds_count", 0.0)
        avg_ttft = round(ttft_sum / ttft_cnt, 2) if ttft_cnt > 0 else 0.0

        gen_sum = prom.get("vllm:generation_tokens_total", 0.0)
        e2e_sum = prom.get("vllm:e2e_request_latency_seconds_sum", 0.0)
        speed_tps = round(gen_sum / e2e_sum, 1) if e2e_sum > 0 else 0.0

        pool_gb = fdinfo_gb or 0.0
        active_gb = round(pool_gb * (paged_pct / 100.0), 1)

        return {
            "online": True,
            "badge": "[ROCm 7.x]",
            "model": model_name,
            "host": f"http://{r_host}",
            "port": r_port,
            "running_reqs": running,
            "waiting_reqs": waiting,
            "speed_tps": speed_tps,
            "gtt_pool_gb": round(pool_gb, 1),
            "gtt_pool_pct": 0.0,
            "gtt_active_gb": active_gb,
            "paged_cache_pct": paged_pct,
            "prefix_cache_hit_pct": prefix_hit_pct,
            "preemptions": preemptions,
            "avg_ttft_sec": avg_ttft,
            "flags": ["/dev/kfd", "Continuous Batching"],
            "client_name": "OpenWebUI",
            "client_port": webui_port,
            "vllm_requests": [],
        }
