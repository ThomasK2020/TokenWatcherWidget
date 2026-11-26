"""
Sonde Matérielle AMD Strix Halo / RDNA3.5 (SOW Lot 1 - 01_BACKEND_TELEMETRY_DAEMON.md).
Découverte dynamique sous /sys/class/drm/ et /sys/class/hwmon/ (zéro chemin codé en dur),
mise en cache mémoire des objets Path, lecture /proc/stat, /proc/meminfo, BAT*,
fallback sécurisé sensors -j / amdgpu_top --dump -J (shell=False, timeout=2.0).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


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

        # Variabilisation & auto-calibration du talon repos batterie (power.idle_baseline_watts)
        self._idle_baseline_cfg: Any = "auto"
        self._fallback_idle_w: float = 12.0
        self._idle_max_gpu_busy: int = 5
        self._idle_max_cpu_busy: int = 15
        self._idle_min_window_sec: int = 15
        self._baseline_cache_path: Path = Path("~/.cache/tokenwatcher/power_baseline.json").expanduser()
        self._cached_baselines: Dict[str, float] = {}
        self._calm_samples: Dict[str, List[float]] = {}
        self._last_cache_save_ts: float = 0.0
        self._last_bat_wh_now: Optional[float] = None
        self._last_bat_w_dis: Optional[float] = None
        self._last_bat_discharging: bool = False
        self._load_baseline_cache()

    def configure_power(self, power_cfg: Optional[Dict[str, Any]]) -> None:
        """Configurer la calibration d'autonomie au repos depuis config/tokenwatcher.yaml."""
        if not isinstance(power_cfg, dict):
            return
        self._idle_baseline_cfg = power_cfg.get("idle_baseline_watts", "auto")
        try:
            fb = float(power_cfg.get("fallback_idle_watts", 12.0))
            if fb > 0:
                self._fallback_idle_w = fb
        except (TypeError, ValueError):
            pass
        try:
            self._idle_max_gpu_busy = max(0, int(power_cfg.get("idle_max_gpu_busy_pct", 5)))
        except (TypeError, ValueError):
            pass
        try:
            self._idle_max_cpu_busy = max(0, int(power_cfg.get("idle_max_cpu_busy_pct", 15)))
        except (TypeError, ValueError):
            pass
        try:
            self._idle_min_window_sec = max(5, int(power_cfg.get("idle_min_window_sec", 15)))
        except (TypeError, ValueError):
            pass
        cache_str = str(power_cfg.get("baseline_cache_file") or "~/.cache/tokenwatcher/power_baseline.json").strip()
        if cache_str:
            new_path = Path(cache_str).expanduser()
            if new_path != self._baseline_cache_path:
                self._baseline_cache_path = new_path
                self._load_baseline_cache()

    def _load_baseline_cache(self) -> None:
        if not self._baseline_cache_path.is_file():
            return
        try:
            raw = json.loads(self._baseline_cache_path.read_text(encoding="utf-8"))
            profs = raw.get("profiles") if isinstance(raw, dict) else None
            if isinstance(profs, dict):
                loaded: Dict[str, float] = {}
                for k, v in profs.items():
                    try:
                        fv = float(v)
                        if 1.0 <= fv <= 150.0:
                            loaded[str(k)] = round(fv, 2)
                    except (TypeError, ValueError):
                        continue
                self._cached_baselines = loaded
        except (OSError, json.JSONDecodeError):
            pass

    def _save_baseline_cache(self) -> None:
        now = time.time()
        if (now - self._last_cache_save_ts) < 10.0:
            return
        try:
            self._baseline_cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._baseline_cache_path.with_suffix(".tmp")
            payload = {
                "updated_at": round(now, 1),
                "profiles": self._cached_baselines,
            }
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp_path, self._baseline_cache_path)
            self._last_cache_save_ts = now
        except OSError:
            pass

    def _read_platform_profile(self) -> str:
        prof = self._read_text(Path("/sys/firmware/acpi/platform_profile"))
        return prof.lower() if prof else "default"

    def resolve_idle_baseline(
        self,
        *,
        is_discharging: bool,
        w_dis: Optional[float],
        gpu_busy_pct: int,
        cpu_busy_pct: int,
        engines_active: bool,
    ) -> tuple[float, str, str]:
        """Résoudre le talon repos en Watts (config fixe, auto-calibré par profil ACPI ou fallback)."""
        platform_profile = self._read_platform_profile()
        cfg_val = self._idle_baseline_cfg

        if not (isinstance(cfg_val, str) and cfg_val.strip().lower() == "auto"):
            try:
                fixed_w = float(cfg_val)
                if fixed_w > 0:
                    return round(fixed_w, 2), "config", platform_profile
            except (TypeError, ValueError):
                pass

        is_calm = (
            is_discharging
            and w_dis is not None
            and 2.0 <= w_dis <= 120.0
            and not engines_active
            and gpu_busy_pct <= self._idle_max_gpu_busy
            and cpu_busy_pct <= self._idle_max_cpu_busy
        )
        if is_calm and w_dis is not None:
            buf = self._calm_samples.setdefault(platform_profile, [])
            buf.append(float(w_dis))
            if len(buf) > 60:
                buf[:] = buf[-60:]
            if len(buf) >= self._idle_min_window_sec:
                sorted_vals = sorted(buf)
                idx = max(0, min(len(sorted_vals) - 1, int(len(sorted_vals) * 0.25)))
                calibrated = round(sorted_vals[idx], 2)
                prev = self._cached_baselines.get(platform_profile)
                self._cached_baselines[platform_profile] = calibrated
                if prev is None or abs(calibrated - prev) >= 0.2:
                    self._save_baseline_cache()
        elif engines_active or gpu_busy_pct > (self._idle_max_gpu_busy * 2 + 5):
            # Éviter de mélanger les transitoires post-charge dans la fenêtre calme en cours
            self._calm_samples.pop(platform_profile, None)

        if platform_profile in self._cached_baselines:
            return self._cached_baselines[platform_profile], "calibrated", platform_profile
        return round(self._fallback_idle_w, 2), "fallback", platform_profile

    def finalize_power_baseline(self, metrics: Dict[str, Any], engines_active: bool) -> None:
        """Mettre à jour la calibration idle batterie avec l'état réel des moteurs locaux."""
        gpu_busy = int(metrics.get("gpu_busy_pct", 0) or 0)
        cpu_busy = int(metrics.get("cpu_busy_pct", 0) or 0)
        baseline_w, baseline_src, plat_prof = self.resolve_idle_baseline(
            is_discharging=self._last_bat_discharging,
            w_dis=self._last_bat_w_dis,
            gpu_busy_pct=gpu_busy,
            cpu_busy_pct=cpu_busy,
            engines_active=engines_active,
        )
        metrics["idle_baseline_watts"] = round(baseline_w, 1)
        metrics["idle_baseline_source"] = baseline_src
        metrics["platform_profile"] = plat_prof
        if (
            metrics.get("has_battery")
            and self._last_bat_discharging
            and self._last_bat_wh_now is not None
            and baseline_w > 0
        ):
            metrics["idle_runtime_remaining"] = self._format_duration_hours(self._last_bat_wh_now / baseline_w)

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

        # Batterie / Workstation sans batterie
        has_battery = c.battery_dir is not None
        battery_pct = 100
        battery_wh = 0
        battery_discharge_w = 0
        ai_runtime_remaining = "AC" if has_battery else "Workstation AC"
        idle_runtime_remaining = "AC"
        self._last_bat_wh_now = None
        self._last_bat_w_dis = None
        self._last_bat_discharging = False
        platform_profile = self._read_platform_profile()
        baseline_w = self._cached_baselines.get(platform_profile, self._fallback_idle_w)
        baseline_src = "calibrated" if platform_profile in self._cached_baselines else "fallback"

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
                self._last_bat_wh_now = en_now_uw / 1_000_000.0
            elif en_full_uw is not None:
                battery_wh = round((en_full_uw / 1_000_000) * (battery_pct / 100.0))
                self._last_bat_wh_now = (en_full_uw / 1_000_000.0) * (battery_pct / 100.0)

            if p_now_uw is not None and p_now_uw > 0 and "discharging" in status_str:
                w_dis = p_now_uw / 1_000_000.0
                self._last_bat_w_dis = w_dis
                self._last_bat_discharging = True
                battery_discharge_w = -round(w_dis)
                if self._last_bat_wh_now is not None:
                    ai_runtime_remaining = self._format_duration_hours(self._last_bat_wh_now / w_dis)
                    idle_runtime_remaining = self._format_duration_hours(self._last_bat_wh_now / max(1.0, baseline_w))
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
            "has_battery": has_battery,
            "battery_pct": battery_pct,
            "battery_wh": battery_wh,
            "battery_discharge_w": battery_discharge_w,
            "ai_runtime_remaining": ai_runtime_remaining,
            "idle_runtime_remaining": idle_runtime_remaining,
            "idle_baseline_watts": round(baseline_w, 1),
            "idle_baseline_source": baseline_src,
            "platform_profile": platform_profile,
            "_meta": {
                "cpu_model": self._cpu_model,
                "gpu_model": self._gpu_model,
                "gfx_arch": self._gfx_arch,
                "mem_total_gb": round(mem_total_gb, 1),
                "mem_available_gb": round(mem_avail_gb, 1),
                "os_used_gb": round(max(0.0, mem_total_gb - mem_avail_gb), 1),
            },
        }
