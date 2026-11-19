"""
Sonde HTTP Multi-Moteurs : Lemonade Vulkan (/slots) & vLLM ROCm (/metrics).
Objectif pour OpenCode :
- Charger les ports dynamiques depuis config/ports.yaml.
- Interroger en asynchrone non-bloquant (timeout=1.5s) :
  * Vulkan llama-server (:8001/slots) -> KV slot usage, PP speed, TG speed, flags actifs.
  * vLLM ROCm (:8000/metrics) -> PagedAttention % usage, num_requests_running, requêtes batch.
- Inspecter /proc/<pid>/fdinfo pour isoler l'allocation mémoire UMA par moteur (Vulkan vs ROCm).
"""
from typing import Dict, Any


class EnginesHttpProbe:
    def __init__(self, ports_config: Dict[str, Any]) -> None:
        self.ports_config = ports_config

    async def poll_vulkan_slots(self) -> Dict[str, Any]:
        """TODO (SOW Lot 1): Interroger GET /slots sur le port Vulkan configuré."""
        raise NotImplementedError("TODO SOW Lot 1: Implémenter poll_vulkan_slots()")

    async def poll_vllm_metrics(self) -> Dict[str, Any]:
        """TODO (SOW Lot 1): Interroger GET /metrics (Prometheus) sur le port vLLM configuré."""
        raise NotImplementedError("TODO SOW Lot 1: Implémenter poll_vllm_metrics()")
