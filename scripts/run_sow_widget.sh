#!/usr/bin/env bash
set -euo pipefail

WIDGET_DIR="${HOME}/data/AI-workspace/TokenWatcherWidget/Widget"
SOW_FILE="${1:-SOW/specs/01_BACKEND_TELEMETRY_DAEMON.md}"

cd "${WIDGET_DIR}"

if [ ! -f "${SOW_FILE}" ]; then
    echo "Erreur : Fichier SOW introuvable (${SOW_FILE})" >&2
    exit 1
fi

PROMPT="Exécute le cahier des charges '${SOW_FILE}' en respectant strictement 'AGENTS.md', 'SOW/00_AGENT_CODING_RULES.md', 'config/ports.yaml' et 'config/i18n_labels.json'.
1. Inspecte les squelettes cibles dans 'src/' et 'ui/index.html' (V4.0.2).
2. Implémente chirurgicalement les méthodes marquées TODO sans aucun port ni chemin sysfs codé en dur (shell=False, SQLite ?mode=ro, Stdlib + PyYAML uniquement).
3. Vérifie la compilation Python (./.venv/bin/python3 -m py_compile) et affiche le rapport de validation avant commit."

opencode run "${PROMPT}"
