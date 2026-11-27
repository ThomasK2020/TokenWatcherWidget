# TokenWatcher Cockpit V2.5.5 MULTI-ENGINE

Moniteur temps réel (1,0 s) pour stations IA locales AMD (Vulkan `llama-server`, ROCm `vLLM`, OpenCode, OpenWebUI) et quotas Cloud (Hermes Agent), combinant un démon Python (`stdlib` + `PyYAML`) et une interface native GTK4 / WebKitGTK 6.0.

## Prérequis système (Ubuntu / GNOME)

```bash
sudo apt-get update && sudo apt-get install -y \
  python3-venv python3-gi python3-yaml \
  gir1.2-gtk-4.0 gir1.2-webkit-6.0 gir1.2-javascriptcoregtk-6.0
```

## Installation (multi-postes)

```bash
git clone https://github.com/ThomasK2020/TokenWatcherWidget.git
cd TokenWatcherWidget
chmod +x scripts/*.sh
./scripts/install_local_widget.sh
systemctl --user enable --now tokenwatcher-daemon.service
```

`install_local_widget.sh` crée l'environnement Python hybride `.venv` (`--system-site-packages`), installe les services `systemd --user` avec le chemin exact du dépôt et génère le lanceur GNOME **TokenWatcher Cockpit V2.5.5**.

## Lancement

- **Mode Production (défaut, sans contrôles de prévisualisation de topologie)** :
  ```bash
  ./scripts/start_widget.sh
  ```
- **Mode Préproduction (affiche les pastilles de sélection de topologie `1..4`)** :
  ```bash
  ./scripts/start_widget.sh --preprod
  ```

## Configuration

Éditez `config/tokenwatcher.yaml` pour adapter les ports des services (`proxy`, `vulkan`, `vllm_rocm`, `open_webui`, `lemonade_admin`, `ollama`), la calibration repos batterie (`power.idle_baseline_watts`), l’efficacité (`efficiency`), le fichier IPC (`/tmp/tokenwatcher_widget_state.json`) ou les plafonds Cloud (`tpm_ceiling`, `rpm_ceiling`, `rpd_ceiling`), puis redémarrez le démon :

```bash
systemctl --user restart tokenwatcher-daemon.service
```
