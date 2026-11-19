"""
Sonde Matérielle AMD Strix Halo / RDNA3.5 (SOW Lot 1 - 01_BACKEND_TELEMETRY_DAEMON.md).
Découverte dynamique sous /sys/class/drm/ et /sys/class/hwmon/ (zéro chemin codé en dur),
mise en cache mémoire des objets Path, lecture /proc/stat, /proc/meminfo, BAT*,
fallback sécurisé sensors -j / amdgpu_top --dump -J (shell=False, timeout=2.0).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class CachedSensorPaths:
    gpu_edge_temp: Optional[Path] = None
    soc_tctl_temp: Optional[Path] = None
    vram_used: Optional[Path] = None
    vram_total: Optional[Path] = None
    gtt_used: Optional[Path] = None
    gtt_total: Optional[Path] = None
    gpu_busy: Optional[Path] = None
    gfx_power: Optional[Path] = None
    gpu_clock: Optional[Path] = None
    battery_dir: Optional[Path] = None


class AmdSysfsProbe:
    def __init__(self) -> None:
        self._cache = CachedSensorPaths()
        self._discovered = False
        self._history_temp: list[int] = []
        self._history_watts: list[int] = []
        self._peak_temp_c: int = 0
        self._prev_cpu_times: tuple[int, int] | None = None
        self._cpu_model: str = "AMD Ryzen AI"
        self._gpu_model: str = "AMD Radeon GPU"
        self._gfx_arch: str = "gfx115x"

    @staticmethod
    def _read_text(path: Optional[Path]) -> Optional[str]:
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            return None

    @staticmethod
    def _read_int(path: Optional[Path]) -> Optional[int]:
        raw = AmdSysfsProbe._read_text(path)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _discover_silicon_metadata(self, card_device: Optional[Path]) -> None:
        cpuinfo = self._read_text(Path("/proc/cpuinfo"))
        if cpuinfo:
            for line in cpuinfo.splitlines():
                if line.lower().startswith("model name"):
                    raw_cpu = line.split(":", 1)[1].strip()
                    # Extraire modèle CPU et GPU intégré si mentionné ("AMD RYZEN AI MAX+ 395 w/ Radeon 8060S")
                    if " w/ " in raw_cpu:
                        cpu_part, gpu_part = raw_cpu.split(" w/ ", 1)
                        self._cpu_model = re.sub(r"^AMD\s+", "", cpu_part, flags=re.I).strip()
                        self._gpu_model = gpu_part.strip()
                    else:
                        self._cpu_model = re.sub(r"^AMD\s+", "", raw_cpu, flags=re.I).strip()
                    break

        # Découverte architecture GFX via /sys/class/kfd/kfd/topology/nodes/*/properties
        for prop_path in sorted(Path("/sys/class/kfd/kfd/topology/nodes").glob("*/properties")):
            txt = self._read_text(prop_path)
            if not txt:
                continue
            target_ver = 0
            simd_count = 0
            for line in txt.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0] == "gfx_target_version":
                    try:
                        target_ver = int(parts[1])
                    except ValueError:
                        target_ver = 0
                elif len(parts) == 2 and parts[0] == "simd_count":
                    try:
                        simd_count = int(parts[1])
                    except ValueError:
                        simd_count = 0
            if target_ver > 0 and simd_count > 0:
                major = target_ver // 10000
                minor = (target_ver // 100) % 100
                stepping = target_ver % 100
                self._gfx_arch = f"gfx{major}{minor}{stepping}"
                break

        if self._gpu_model == "AMD Radeon GPU" and card_device is not None:
            uevent = self._read_text(card_device / "uevent") or ""
            if "1002:1586" in uevent:
                self._gpu_model = "Radeon 8060S"

    def discover_sensors(self) -> CachedSensorPaths:
        """Parcourir /sys/class/drm et /sys/class/hwmon sans indice codé en dur."""
        cache = CachedSensorPaths()
        amd_device: Optional[Path] = None

        for card in sorted(Path("/sys/class/drm").glob("card*")):
            # Ignorer les connecteurs type cardX-DP-1
            if "-" in card.name:
                continue
            device_path = card / "device"
            vendor = self._read_text(device_path / "vendor")
            if vendor != "0x1002":
                continue
            amd_device = device_path

            for attr, filename in (
                ("vram_used", "mem_info_vram_used"),
                ("vram_total", "mem_info_vram_total"),
                ("gtt_used", "mem_info_gtt_used"),
                ("gtt_total", "mem_info_gtt_total"),
                ("gpu_busy", "gpu_busy_percent"),
            ):
                p = device_path / filename
                if p.exists():
                    setattr(cache, attr, p)

            for hwmon in sorted(device_path.glob("hwmon/hwmon*")):
                # Température GPU Edge (chercher temp*_label == "edge")
                for lbl_path in sorted(hwmon.glob("temp*_label")):
                    lbl = (self._read_text(lbl_path) or "").lower()
                    if lbl == "edge":
                        inp = hwmon / lbl_path.name.replace("_label", "_input")
                        if inp.exists():
                            cache.gpu_edge_temp = inp
                            break
                if cache.gpu_edge_temp is None and (hwmon / "temp1_input").exists():
                    cache.gpu_edge_temp = hwmon / "temp1_input"

                for p_name in ("power1_average", "power1_input"):
                    p_path = hwmon / p_name
                    if p_path.exists():
                        cache.gfx_power = p_path
                        break

                if (hwmon / "freq1_input").exists():
                    cache.gpu_clock = hwmon / "freq1_input"

            break

        # Découverte SoC Tctl (k10temp / zenpower) sous /sys/class/hwmon/hwmon*
        for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
            name = (self._read_text(hwmon / "name") or "").lower()
            if name in {"k10temp", "zenpower"}:
                for lbl_path in sorted(hwmon.glob("temp*_label")):
                    if (self._read_text(lbl_path) or "").lower() == "tctl":
                        inp = hwmon / lbl_path.name.replace("_label", "_input")
                        if inp.exists():
                            cache.soc_tctl_temp = inp
                            break
                if cache.soc_tctl_temp is None and (hwmon / "temp1_input").exists():
                    cache.soc_tctl_temp = hwmon / "temp1_input"
                break

        # Découverte Batterie
        for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
            if (bat / "capacity").exists() or (bat / "energy_now").exists():
                cache.battery_dir = bat
                break

        self._discover_silicon_metadata(amd_device)
        self._cache = cache
        self._discovered = True
        return self._cache

    def _read_cpu_busy_pct(self) -> int:
        raw = self._read_text(Path("/proc/stat"))
        if not raw:
            return 0
        first = raw.splitlines()[0].split()
        if len(first) < 5 or first[0] != "cpu":
            return 0
        vals = [int(x) for x in first[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        if self._prev_cpu_times is None:
            self._prev_cpu_times = (idle, total)
            return 0
        prev_idle, prev_total = self._prev_cpu_times
        self._prev_cpu_times = (idle, total)
        d_total = total - prev_total
        d_idle = idle - prev_idle
        if d_total <= 0:
            return 0
        return max(0, min(100, round((1.0 - (d_idle / d_total)) * 100)))

    def _read_meminfo_gb(self) -> tuple[float, float]:
        raw = self._read_text(Path("/proc/meminfo"))
        if not raw:
            return 0.0, 0.0
        total_kb = 0
        avail_kb = 0
        for line in raw.splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
        return total_kb / (1024 * 1024), avail_kb / (1024 * 1024)

    def _fallback_sensors_json(self) -> tuple[Optional[float], Optional[float], Optional[float]]:
        sensors_bin = shutil.which("sensors")
        if not sensors_bin:
            return None, None, None
        try:
            res = subprocess.run(
                [sensors_bin, "-j"],
                shell=False,
                timeout=2.0,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0 or not res.stdout.strip():
                return None, None, None
            parsed = json.loads(res.stdout)
        except Exception:
            return None, None, None

        edge_c: Optional[float] = None
        tctl_c: Optional[float] = None
        power_w: Optional[float] = None
        for chip, payload in parsed.items():
            if not isinstance(payload, dict):
                continue
            low = chip.lower()
            if "amdgpu" in low:
                for k, v in payload.items():
                    if isinstance(v, dict):
                        if "edge" in k.lower() and "temp1_input" in v:
                            edge_c = float(v["temp1_input"])
                        elif "ppt" in k.lower() or "power" in k.lower():
                            for pk, pv in v.items():
                                if pk.endswith("_average") or pk.endswith("_input"):
                                    power_w = float(pv)
                                    break
            elif "k10temp" in low or "zenpower" in low:
                for k, v in payload.items():
                    if isinstance(v, dict) and "tctl" in k.lower():
                        for tk, tv in v.items():
                            if tk.endswith("_input"):
                                tctl_c = float(tv)
                                break
        return edge_c, tctl_c, power_w

    def _fallback_amdgpu_top_busy(self) -> Optional[int]:
        bin_path = shutil.which("amdgpu_top")
        if not bin_path:
            return None
        try:
            res = subprocess.run(
                [bin_path, "--dump", "-J"],
                shell=False,
                timeout=2.0,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0 or not res.stdout.strip():
                return None
            parsed = json.loads(res.stdout)
            devices = parsed if isinstance(parsed, list) else parsed.get("devices", [])
            if devices and isinstance(devices[0], dict):
                act = devices[0].get("gpu_activity", {})
                if isinstance(act, dict) and "GFX" in act:
                    val = act["GFX"]
                    return int(val.get("value", 0) if isinstance(val, dict) else val)
        except Exception:
            return None
        return None

    @staticmethod
    def _format_duration_hours(hours: float) -> str:
        if hours <= 0 or hours > 99:
            return "--"
        total_min = int(round(hours * 60))
        h, m = divmod(total_min, 60)
        return f"{h}h {m:02d}m"

    def read_metrics(self) -> Dict[str, Any]:
        """Lire les sondes en cache et retourner le dictionnaire hardware."""
        if not self._discovered:
            self.discover_sensors()

        c = self._cache
        edge_raw = self._read_int(c.gpu_edge_temp)
        tctl_raw = self._read_int(c.soc_tctl_temp)
        power_raw = self._read_int(c.gfx_power)
        freq_raw = self._read_int(c.gpu_clock)
        busy_raw = self._read_int(c.gpu_busy)

        edge_c = (edge_raw / 1000.0) if edge_raw is not None else None
        tctl_c = (tctl_raw / 1000.0) if tctl_raw is not None else None
        power_w = (power_raw / 1_000_000.0) if power_raw is not None else None

        if edge_c is None or tctl_c is None or power_w is None:
            fb_edge, fb_tctl, fb_power = self._fallback_sensors_json()
            if edge_c is None:
                edge_c = fb_edge
            if tctl_c is None:
                tctl_c = fb_tctl
            if power_w is None:
                power_w = fb_power

        if busy_raw is None:
            busy_raw = self._fallback_amdgpu_top_busy()

        gpu_edge_temp_c = round(edge_c if edge_c is not None else 0.0, 1)
        soc_tctl_temp_c = round(tctl_c if tctl_c is not None else 0.0, 1)
        gfx_power_w = round(power_w if power_w is not None else 0.0, 1)
        gpu_clock_mhz = round(freq_raw / 1_000_000) if freq_raw is not None else 0
        gpu_busy_pct = max(0, min(100, busy_raw if busy_raw is not None else 0))

        vram_used_b = self._read_int(c.vram_used) or 0
        vram_total_b = self._read_int(c.vram_total) or 0
        gtt_used_b = self._read_int(c.gtt_used) or 0
        gtt_total_b = self._read_int(c.gtt_total) or 0

        vram_used_gb = round(vram_used_b / (1024 ** 3), 2)
        vram_total_gb = round(vram_total_b / (1024 ** 3), 2)
        vram_pct = round((vram_used_b / vram_total_b) * 100) if vram_total_b > 0 else 0
        vram_free_gb = round(max(0.0, vram_total_gb - vram_used_gb), 2)

        gtt_used_gb = round(gtt_used_b / (1024 ** 3), 2)
        gtt_total_gb = round(gtt_total_b / (1024 ** 3), 2)
        gtt_pct = round((gtt_used_b / gtt_total_b) * 100) if gtt_total_b > 0 else 0
        gtt_free_gb = round(max(0.0, gtt_total_gb - gtt_used_gb), 2)

        cpu_busy_pct = self._read_cpu_busy_pct()
        mem_total_gb, mem_avail_gb = self._read_meminfo_gb()

        # Historique 8 points & vecteurs 3-points avec hystérésis
        t_int = int(round(gpu_edge_temp_c))
        w_int = int(round(gfx_power_w))
        self._peak_temp_c = max(self._peak_temp_c, t_int)
        self._history_temp.append(t_int)
        self._history_temp = self._history_temp[-8:]
        self._history_watts.append(w_int)
        self._history_watts = self._history_watts[-8:]

        t_hist = self._history_temp if len(self._history_temp) >= 2 else [t_int, t_int]
        t_start = t_hist[0]
        t_mid = t_hist[len(t_hist) // 2]
        t_arrow = "↗" if (t_int - t_mid) > 1.5 else ("↘" if (t_int - t_mid) < -1.5 else "→")
        temp_vector_3pt = f"[{t_start}° → {t_mid}° → {t_int}°C {t_arrow} Pic {self._peak_temp_c}°C]"

        w_hist = self._history_watts if len(self._history_watts) >= 2 else [w_int, w_int]
        w_start = w_hist[0]
        w_mid = w_hist[len(w_hist) // 2]
        w_avg = round(sum(w_hist) / len(w_hist))
        w_arrow = "↗" if (w_int - w_mid) > 2 else ("↘" if (w_int - w_mid) < -2 else "→")
        power_vector_3pt = f"[{w_start}W → {w_mid}W → {w_int}W {w_arrow} Moy {w_avg}W]"

        if gpu_edge_temp_c >= 85.0:
            gpu_temp_status = "CRIT"
        elif gpu_edge_temp_c >= 65.0:
            gpu_temp_status = "WARN"
        else:
            gpu_temp_status = "OK"

        # Batterie
        battery_pct = 100
        battery_wh = 0
        battery_discharge_w = 0
        ai_runtime_remaining = "AC"
        idle_runtime_remaining = "AC"

        if c.battery_dir is not None:
            cap = self._read_int(c.battery_dir / "capacity")
            if cap is not None:
                battery_pct = max(0, min(100, cap))
            en_now_uw = self._read_int(c.battery_dir / "energy_now")
            en_full_uw = self._read_int(c.battery_dir / "energy_full")
            p_now_uw = self._read_int(c.battery_dir / "power_now")
            status_str = (self._read_text(c.battery_dir / "status") or "").lower()

            if en_now_uw is not None:
                battery_wh = round(en_now_uw / 1_000_000)
            elif en_full_uw is not None:
                battery_wh = round((en_full_uw / 1_000_000) * (battery_pct / 100.0))

            if p_now_uw is not None and p_now_uw > 0 and "discharging" in status_str:
                w_dis = p_now_uw / 1_000_000.0
                battery_discharge_w = -round(w_dis)
                if en_now_uw is not None:
                    wh_now = en_now_uw / 1_000_000.0
                    ai_runtime_remaining = self._format_duration_hours(wh_now / w_dis)
                    idle_runtime_remaining = self._format_duration_hours(wh_now / 12.0)
            else:
                battery_discharge_w = 0
                ai_runtime_remaining = "AC"
                idle_runtime_remaining = "AC"

        return {
            "offload_gpu_pct": 100 if (vram_used_gb > 0.2 or gpu_busy_pct > 0) else 0,
            "vram_used_gb": round(vram_used_gb, 1),
            "vram_total_gb": round(vram_total_gb, 1),
            "vram_pct": vram_pct,
            "vram_free_gb": round(vram_free_gb, 1),
            "gtt_used_gb": round(gtt_used_gb, 1),
            "gtt_total_gb": round(gtt_total_gb, 1),
            "gtt_pct": gtt_pct,
            "gtt_free_gb": round(gtt_free_gb, 1),
            "gpu_busy_pct": gpu_busy_pct,
            "gpu_clock_mhz": gpu_clock_mhz,
            "cpu_busy_pct": cpu_busy_pct,
            "gpu_edge_temp_c": round(gpu_edge_temp_c),
            "gpu_temp_status": gpu_temp_status,
            "soc_tctl_temp_c": round(soc_tctl_temp_c),
            "temp_vector_3pt": temp_vector_3pt,
            "history_temps_c": list(self._history_temp),
            "gfx_power_w": round(gfx_power_w),
            "power_vector_3pt": power_vector_3pt,
            "history_watts": list(self._history_watts),
            "efficiency_tok_per_j": 0.0,  # Calculé par TokenWatcherDaemon avec tg_speed réel
            "battery_pct": battery_pct,
            "battery_wh": battery_wh,
            "battery_discharge_w": battery_discharge_w,
            "ai_runtime_remaining": ai_runtime_remaining,
            "idle_runtime_remaining": idle_runtime_remaining,
            "_meta": {
                "cpu_model": self._cpu_model,
                "gpu_model": self._gpu_model,
                "gfx_arch": self._gfx_arch,
                "mem_total_gb": round(mem_total_gb, 1),
                "mem_available_gb": round(mem_avail_gb, 1),
                "os_used_gb": round(max(0.0, mem_total_gb - mem_avail_gb), 1),
            },
        }
