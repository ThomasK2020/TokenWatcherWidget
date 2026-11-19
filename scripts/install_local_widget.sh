#!/usr/bin/env bash
set -euo pipefail

WIDGET_DIR="${HOME}/data/AI-workspace/TokenWatcherWidget/Widget"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
DESKTOP_DIR="${HOME}/.local/share/applications"

mkdir -p "${USER_SYSTEMD_DIR}" "${DESKTOP_DIR}"

cp -f "${WIDGET_DIR}/systemd/tokenwatcher-daemon.service" "${USER_SYSTEMD_DIR}/tokenwatcher-daemon.service"
cp -f "${WIDGET_DIR}/systemd/tokenwatcher-widget.service" "${USER_SYSTEMD_DIR}/tokenwatcher-widget.service"

cat > "${DESKTOP_DIR}/tokenwatcher-widget.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=TokenWatcher Cockpit V4.0.2
Comment=Monitoring Multi-Engine LLM & AMD Strix Halo
Exec=${WIDGET_DIR}/scripts/start_widget.sh
Terminal=false
Categories=System;Monitor;Utility;
StartupWMClass=io.thomas.tokenwatcher.widget
EOF

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${DESKTOP_DIR}" || true
fi

systemctl --user daemon-reload

echo "✅ Services installés dans ${USER_SYSTEMD_DIR}"
echo "✅ Lanceur GNOME créé dans ${DESKTOP_DIR}/tokenwatcher-widget.desktop"
