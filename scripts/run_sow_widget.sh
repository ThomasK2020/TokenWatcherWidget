#!/usr/bin/env bash
set -euo pipefail

WIDGET_DIR="${HOME}/data/AI-workspace/TokenWatcherWidget/Widget"
SOW_FILE="${1:-SOW/specs/01_BACKEND_TELEMETRY_DAEMON.md}"

cd "${WIDGET_DIR}"

if [ ! -f "${SOW_FILE}" ]; then
    echo "Erreur : Fichier SOW introuvable (${SOW_FILE})" >&2
    exit 1
fi

if [[ "${SOW_FILE}" == *"02_FRONTEND"* ]]; then
    TARGET_SCOPE="Inspecte uniquement 'src/widget_runner.py', 'config/i18n_labels.json' et les sections JS/DOM ciblées dans 'ui/index.html' (V4.0.2, en conservant le contexte <64k)."
elif [[ "${SOW_FILE}" == *"03_SYSTEMD"* ]]; then
    TARGET_SCOPE="Inspecte uniquement 'systemd/' et 'scripts/'."
else
    TARGET_SCOPE="Inspecte uniquement les squelettes cibles dans 'src/' et 'src/probes/' (ne charge pas 'ui/index.html' afin de préserver la fenêtre 64k)."
fi

PROMPT="Exécute le cahier des charges '${SOW_FILE}' en respectant strictement 'AGENTS.md', 'SOW/00_AGENT_CODING_RULES.md', 'config/ports.yaml' et 'config/i18n_labels.json'.
1. ${TARGET_SCOPE}
2. Implémente chirurgicalement les méthodes marquées TODO sans aucun port ni chemin sysfs codé en dur (shell=False, SQLite ?mode=ro, Stdlib + PyYAML uniquement).
3. Vérifie la compilation Python (./.venv/bin/python3 -m py_compile src/*.py src/probes/*.py) et affiche le rapport de validation avant commit."

opencode run "${PROMPT}"
