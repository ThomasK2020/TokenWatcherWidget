#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WIDGET_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
DESKTOP_DIR="${HOME}/.local/share/applications"
LOCAL_TYPELIB="${HOME}/.local/lib/girepository-1.0"

if [ ! -x "${WIDGET_DIR}/.venv/bin/python3" ]; then
    /usr/bin/python3 -m venv --system-site-packages "${WIDGET_DIR}/.venv"
fi

mkdir -p "${USER_SYSTEMD_DIR}" "${DESKTOP_DIR}"

cat > "${USER_SYSTEMD_DIR}/tokenwatcher-daemon.service" <<EOF
[Unit]
Description=TokenWatcher Telemetry Daemon V4.2.0 MULTI-ENGINE
After=network.target

[Service]
Type=simple
WorkingDirectory=${WIDGET_DIR}
ExecStart=${WIDGET_DIR}/.venv/bin/python3 src/tokenwatcher_daemon.py
Restart=always
RestartSec=2s
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

cat > "${USER_SYSTEMD_DIR}/tokenwatcher-widget.service" <<EOF
[Unit]
Description=TokenWatcher GTK4 Cockpit Widget V4.2.0
After=graphical-session.target tokenwatcher-daemon.service
Wants=tokenwatcher-daemon.service
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory=${WIDGET_DIR}
PassEnvironment=WAYLAND_DISPLAY DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
Environment=GI_TYPELIB_PATH=${LOCAL_TYPELIB}
ExecStart=${WIDGET_DIR}/.venv/bin/python3 src/widget_runner.py
Restart=on-failure
RestartSec=3s

[Install]
WantedBy=graphical-session.target
EOF

cat > "${DESKTOP_DIR}/tokenwatcher-widget.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=TokenWatcher Cockpit V4.2.0
Comment=Monitoring Multi-Engine LLM & AMD Radeon APU
Exec=${WIDGET_DIR}/scripts/start_widget.sh
Terminal=false
Categories=System;Monitor;Utility;
StartupWMClass=io.thomas.tokenwatcher.widget
EOF

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${DESKTOP_DIR}" || true
fi

systemctl --user daemon-reload

echo "✅ Services installés dans ${USER_SYSTEMD_DIR} (WIDGET_DIR=${WIDGET_DIR})"
echo "✅ Lanceur GNOME créé dans ${DESKTOP_DIR}/tokenwatcher-widget.desktop"
