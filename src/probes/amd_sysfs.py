"""
Sonde Matérielle AMD Strix Halo / RDNA3.5 (SOW Lot 1 - 01_GPU_TEMP_TELEMETRY.md).
Objectif pour OpenCode :
- Découvrir dynamiquement (zéro chemin en dur) :
  * GPU T* Edge (temp1_input sous /sys/class/drm/card*/device/hwmon/hwmon*/)
  * SoC hottest T*Ctl (via driver k10temp sous /sys/class/hwmon/)
  * VRAM dédiée (mem_info_vram_used / mem_info_vram_total)
  * GTT UMA (mem_info_gtt_used / mem_info_gtt_total)
  * GPU Busy % (gpu_busy_percent) & Consommation GFX Watts (power1_average / power1_input)
- Mettre en cache mémoire les objets Path résolus au démarrage.
- Calculer les vecteurs de tendance à 3 points avec hystérésis (↗, ↘, →).
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional


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


class AmdSysfsProbe:
    def __init__(self) -> None:
        self._cache = CachedSensorPaths()
        self._history_temp: list[float] = []
        self._history_watts: list[float] = []

    def discover_sensors(self) -> CachedSensorPaths:
        """TODO (SOW Lot 1): Parcourir /sys/class/drm et /sys/class/hwmon sans indice codé en dur."""
        raise NotImplementedError("TODO SOW Lot 1: Implémenter discover_sensors()")

    def read_metrics(self) -> Dict[str, Any]:
        """TODO (SOW Lot 1): Lire les sondes en cache et retourner le dictionnaire hardware."""
        raise NotImplementedError("TODO SOW Lot 1: Implémenter read_metrics()")
