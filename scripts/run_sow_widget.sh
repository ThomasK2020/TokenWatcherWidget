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
    TARGET_SCOPE="Inspecte uniquement 'src/widget_runner.py', 'config/i18n_labels.json' et les plages ciblées dans 'ui/index.html' (lignes 880-1060 et 1072-2226, sans tout lire d'un bloc pour rester <64k)."
    VERIFY_STEP="Vérifie dans 'ui/index.html' : conservation de 'grid grid-cols-1 gap-4' (zéro lg:grid-cols-2), 'status !== 0' sur fetch file://, fusion Object.assign(DEFAULT_I18N...), et protection des feuilles [data-i18n]."
elif [[ "${SOW_FILE}" == *"03_SYSTEMD"* ]]; then
    TARGET_SCOPE="Inspecte uniquement 'systemd/' et 'scripts/'."
    VERIFY_STEP="Vérifie la syntaxe bash (bash -n scripts/*.sh) et les chemins dans systemd/*.service."
else
    TARGET_SCOPE="Inspecte uniquement les squelettes cibles dans 'src/' et 'src/probes/' (ne charge pas 'ui/index.html' afin de préserver la fenêtre 64k)."
    VERIFY_STEP="Vérifie la compilation Python (./.venv/bin/python3 -m py_compile src/*.py src/probes/*.py) et affiche le rapport de validation avant commit."
fi

PROMPT="Exécute le cahier des charges '${SOW_FILE}' en respectant strictement 'AGENTS.md', 'SOW/00_AGENT_CODING_RULES.md', 'config/tokenwatcher.yaml' et 'config/i18n_labels.json'.
1. ${TARGET_SCOPE}
2. Implémente chirurgicalement les méthodes/fonctions cibles sans aucun port ni chemin sysfs codé en dur (shell=False, SQLite ?mode=ro, Stdlib + PyYAML uniquement).
3. ${VERIFY_STEP}"

opencode run "${PROMPT}"
