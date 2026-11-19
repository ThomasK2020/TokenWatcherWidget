"""
Sonde SQLite Read-Only : OpenCode, Hermes Agent & OpenWebUI.
Objectif pour OpenCode :
- Ouvrir en mode lecture seule stricte (?mode=ro) :
  * ~/.local/share/opencode/opencode.db
  * ~/.hermes/state.db
  * ~/.local/share/open-webui/webui.db
- Extraire les compteurs IN, OUT, RSN (Thought), KV Cache Hit et cachedContentTokenCount.
- Calculer le TPM sur fenêtre glissante de 60 secondes pour prévenir l'état 'Out of Quota' (HTTP 429).
- Détecter automatiquement la topologie active (1, 2, 3 ou 4).
"""
from typing import Dict, Any


class SqliteTelemetryReader:
    def __init__(self) -> None:
        pass

    def read_active_topology_and_tokens(self) -> Dict[str, Any]:
        """TODO (SOW Lot 1): Lire les bases SQLite en ?mode=ro et calculer la fenêtre glissante 60s."""
        raise NotImplementedError("TODO SOW Lot 1: Implémenter read_active_topology_and_tokens()")
