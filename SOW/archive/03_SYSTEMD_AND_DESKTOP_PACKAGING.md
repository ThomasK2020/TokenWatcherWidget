# Spécification Technique — SOW 03 : Services Systemd, Scripts de Lancement & Packaging

## 1. Vue d'Ensemble & Périmètre
Ce document définit l'architecture d'exécution, les scripts d'orchestration/lancement et l'intégration systemd/Desktop/Git pour TokenWatcher Widget V4.0.2 MULTI-ENGINE :
- `scripts/run_sow_widget.sh` : Orchestrateur CLI (`opencode run`) pour l'exécution des lots SOW (`01_BACKEND_TELEMETRY_DAEMON.md`, `02_FRONTEND_IPC_AND_I18N.md`).
- `scripts/start_widget.sh` : Lanceur runtime qui démarre le démon backend si absent puis ouvre le Widget GTK4.
- `scripts/install_local_widget.sh` : Script d'installation locale des services `systemd --user` et du lanceur GNOME `.desktop` (`~/.local/share/applications/tokenwatcher-widget.desktop`).
- `systemd/tokenwatcher-daemon.service` : Service utilisateur Systemd pour `src/tokenwatcher_daemon.py`.
- `systemd/tokenwatcher-widget.service` : Service utilisateur Systemd pour `src/widget_runner.py` (session graphique Wayland/X11).

---

## 2. Environnement d'Exécution & Dépendances

### 2.1 Arborescence & Emplacement Exclusif
- **Répertoire de travail exclusif** :
  `/home/thomas/data/AI-workspace/TokenWatcherWidget/Widget`
- **Interdiction formelle** : Ne jamais lire ni modifier `../tokenwatcher-topbar` ou `../TokenWatcher-TopBar`.

### 2.2 Environnement Python Hybride
- **Venv hybride** : `/home/thomas/data/AI-workspace/TokenWatcherWidget/Widget/.venv` créé avec `--system-site-packages` (accès direct à `gi` et `yaml`).
- **Interpréteur Python** : `/home/thomas/data/AI-workspace/TokenWatcherWidget/Widget/.venv/bin/python3`.
- **Bibliothèques Introspection (Typelibs)** :
  `GI_TYPELIB_PATH=/home/thomas/.local/lib/girepository-1.0:$GI_TYPELIB_PATH`
  Comprenant `WebKit-6.0.typelib` et `JavaScriptCore-6.0.typelib`.

---

## 3. Services Systemd Utilisateur (`systemd/`)

### 3.1 Service Démon Télémesure (`systemd/tokenwatcher-daemon.service`)
```ini
[Unit]
Description=TokenWatcher Telemetry Daemon V4.0.2 MULTI-ENGINE
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/thomas/data/AI-workspace/TokenWatcherWidget/Widget
ExecStart=/home/thomas/data/AI-workspace/TokenWatcherWidget/Widget/.venv/bin/python3 src/tokenwatcher_daemon.py
Restart=always
RestartSec=2s
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

### 3.2 Service Widget GTK4 (`systemd/tokenwatcher-widget.service`)
Pour fonctionner sous systemd `--user` avec GNOME/Wayland (GTK4 + WebKitGTK 6.0), le service doit hériter des variables de session graphique :
```ini
[Unit]
Description=TokenWatcher GTK4 Cockpit Widget V4.0.2
After=graphical-session.target tokenwatcher-daemon.service
Wants=tokenwatcher-daemon.service
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory=/home/thomas/data/AI-workspace/TokenWatcherWidget/Widget
PassEnvironment=WAYLAND_DISPLAY DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
Environment=GI_TYPELIB_PATH=/home/thomas/.local/lib/girepository-1.0
ExecStart=/home/thomas/data/AI-workspace/TokenWatcherWidget/Widget/.venv/bin/python3 src/widget_runner.py
Restart=on-failure
RestartSec=3s

[Install]
WantedBy=graphical-session.target
```

---

## 4. Scripts de Développement & Lancement (`scripts/`)

### 4.1 Orchestrateur SOW (`scripts/run_sow_widget.sh`)
- Script d'automatisation appelant `opencode run` sur un fichier SOW cible (par défaut `SOW/specs/01_BACKEND_TELEMETRY_DAEMON.md`).
- Injecte les contraintes strictes (`AGENTS.md`, `SOW/00_AGENT_CODING_RULES.md`, `config/ports.yaml`, `config/i18n_labels.json`) et demande la validation `python3 -m py_compile` avant commit.

### 4.2 Lanceur Runtime Utilisateur (`scripts/start_widget.sh`)
- Vérifie que `.venv/bin/python3` existe et exporte `GI_TYPELIB_PATH`.
- Démarre `src/tokenwatcher_daemon.py` en arrière-plan (avec nettoyage propre sur `EXIT` si lancé hors systemd) et attend l'apparition de `/tmp/tokenwatcher_widget_state.json`.
- Lance `src/widget_runner.py` au premier plan.

### 4.3 Script d'Installation Locale & Lanceur GNOME `.desktop` (`scripts/install_local_widget.sh`)
- Copie/lie `systemd/tokenwatcher-daemon.service` et `systemd/tokenwatcher-widget.service` vers `~/.config/systemd/user/`.
- Génère le lanceur GNOME `~/.local/share/applications/tokenwatcher-widget.desktop` :
  ```ini
  [Desktop Entry]
  Type=Application
  Name=TokenWatcher Cockpit V4.0.2
  Comment=Monitoring Multi-Engine LLM & AMD Strix Halo
  Exec=/home/thomas/data/AI-workspace/TokenWatcherWidget/Widget/scripts/start_widget.sh
  Terminal=false
  Categories=System;Monitor;Utility;
  StartupWMClass=io.thomas.tokenwatcher.widget
  ```
- Exécute `update-desktop-database ~/.local/share/applications/` (si disponible) et `systemctl --user daemon-reload`.

---

## 5. Intégration Git & Dépôt GitHub

### 5.1 Remote Cible
- **Dépôt distant** : `https://github.com/ThomasK2020/TokenWatcherWidget.git`
- **Branche principale** : `main`

### 5.2 Commandes d'Initialisation & Premier Push
```bash
cd /home/thomas/data/AI-workspace/TokenWatcherWidget/Widget
gh repo create ThomasK2020/TokenWatcherWidget --public --source=. --remote=origin --push
```

---

## 6. Procédure de Validation Système & Recette (SOW Lot 3)
1. **Compilation Python** :
   `.venv/bin/python3 -m py_compile src/*.py src/probes/*.py`
2. **Validation du payload IPC** :
   Vérification de `/tmp/tokenwatcher_widget_state.json` (JSON valide, `schema_version == "4.0.2"`).
3. **Validation de l'interface GTK4 / WebKitGTK 6.0** :
   Lancement via `./scripts/start_widget.sh` (ou `systemd --user`) et vérification visuelle du cockpit V4.0.2.
4. **Validation systemd (après accord utilisateur)** :
   `systemctl --user daemon-reload && systemctl --user enable --now tokenwatcher-daemon.service`
