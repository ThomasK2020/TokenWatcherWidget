#!/usr/bin/env bash
set -euo pipefail

WIDGET_DIR="${HOME}/data/AI-workspace/TokenWatcherWidget/Widget"
PYTHON_BIN="${WIDGET_DIR}/.venv/bin/python3"
STATE_FILE="/tmp/tokenwatcher_widget_state.json"
LOCAL_TYPELIB="${HOME}/.local/lib/girepository-1.0"

if [ ! -x "${PYTHON_BIN}" ]; then
    echo "Erreur : environnement virtuel introuvable (${PYTHON_BIN})" >&2
    exit 1
fi

if [ -d "${LOCAL_TYPELIB}" ]; then
    export GI_TYPELIB_PATH="${LOCAL_TYPELIB}${GI_TYPELIB_PATH:+:${GI_TYPELIB_PATH}}"
fi

cd "${WIDGET_DIR}"

DAEMON_PID=""
cleanup() {
    if [ -n "${DAEMON_PID}" ] && kill -0 "${DAEMON_PID}" 2>/dev/null; then
        kill "${DAEMON_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if ! systemctl --user is-active --quiet tokenwatcher-daemon.service 2>/dev/null; then
    "${PYTHON_BIN}" "${WIDGET_DIR}/src/tokenwatcher_daemon.py" >/tmp/tokenwatcher-widget-daemon.log 2>&1 &
    DAEMON_PID=$!
    for _ in $(seq 1 25); do
        if [ -s "${STATE_FILE}" ]; then
            break
        fi
        sleep 0.2
    done
fi

"${PYTHON_BIN}" "${WIDGET_DIR}/src/widget_runner.py"
