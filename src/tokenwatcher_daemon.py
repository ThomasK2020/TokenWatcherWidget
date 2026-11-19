#!/usr/bin/env python3
"""
Démon Principal TokenWatcher V4.0.2 (SOW Lot 1).
Orchestre les 3 sondes (AmdSysfsProbe, EnginesHttpProbe, SqliteTelemetryReader)
à un intervalle de 1.0s et écrit atomiquement l'état dans /tmp/tokenwatcher_state.json.
"""
import asyncio
from pathlib import Path
from typing import Dict, Any

from probes.amd_sysfs import AmdSysfsProbe
from probes.engines_http import EnginesHttpProbe
from probes.sqlite_readers import SqliteTelemetryReader


class TokenWatcherDaemon:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.sysfs_probe = AmdSysfsProbe()
        self.sqlite_reader = SqliteTelemetryReader()
        self.engines_probe: EnginesHttpProbe | None = None

    def load_ports_config(self) -> Dict[str, Any]:
        """TODO (SOW Lot 1): Charger config/ports.yaml."""
        raise NotImplementedError("TODO SOW Lot 1: Implémenter load_ports_config()")

    async def collect_once(self) -> Dict[str, Any]:
        """TODO (SOW Lot 1): Fusionner les données des 3 sondes dans le schéma JSON V4.0.2."""
        raise NotImplementedError("TODO SOW Lot 1: Implémenter collect_once()")

    def write_ipc_atomic(self, payload: Dict[str, Any]) -> None:
        """TODO (SOW Lot 1): Écrire via fichier temporaire + os.replace vers /tmp/tokenwatcher_state.json."""
        raise NotImplementedError("TODO SOW Lot 1: Implémenter write_ipc_atomic()")

    async def run_forever(self) -> None:
        """Boucle principale 1.0s."""
        raise NotImplementedError("TODO SOW Lot 1: Implémenter run_forever()")


if __name__ == "__main__":
    root_dir = Path(__file__).resolve().parent.parent
    daemon = TokenWatcherDaemon(config_dir=root_dir / "config")
    asyncio.run(daemon.run_forever())
