# Spécification Technique — SOW 01 : Démon Télémesure Backend & Sondes System/LLM

## 1. Vue d'Ensemble & Périmètre
Ce document constitue le cahier des charges exécutable (SOW Lot 1) pour le démon de télémesure backend (`src/tokenwatcher_daemon.py`) et les trois sondes dans `src/probes/` :
- `src/probes/amd_sysfs.py` : `AmdSysfsProbe` + `CachedSensorPaths`
- `src/probes/engines_http.py` : `EnginesHttpProbe`
- `src/probes/sqlite_readers.py` : `SqliteTelemetryReader`
- `src/tokenwatcher_daemon.py` : `TokenWatcherDaemon`

> **Contrainte d'édition stricte (`AGENTS.md` / `SOW/00_AGENT_CODING_RULES.md`)** :
> Implémenter chirurgicalement uniquement les méthodes marquées `TODO (SOW Lot 1)` dans les squelettes existants, sans casser leurs signatures publiques et en conservant exclusivement la Stdlib Python 3.11+ + `PyYAML`.
> Interdiction formelle d'importer `aiohttp`, `httpx`, `requests`, `psutil` ou `pydantic`.

---

## 2. Configuration Dynamique (`config/ports.yaml`)
Toutes les URLs, ports et chemins IPC sont chargés depuis `config/ports.yaml` via `yaml.safe_load()` :
```yaml
services:
  proxy:          { host: "127.0.0.1", port: 13306, unit: "lemonade-opencode-proxy.service" }
  vulkan:         { host: "127.0.0.1", port: 8001, slots_endpoint: "/slots", metrics_endpoint: "/metrics" }
  lemonade_admin: { host: "127.0.0.1", port: 13305, health_endpoint: "/api/v1/health" }
  vllm_rocm:      { host: "127.0.0.1", port: 8000, metrics_endpoint: "/metrics", models_endpoint: "/v1/models" }
  open_webui:     { host: "127.0.0.1", port: 8080 }
ipc:
  state_file: "/tmp/tokenwatcher_widget_state.json"
  polling_interval_sec: 1.0
```
*Dégradation gracieuse* : si `config/ports.yaml` manque une clé, utiliser les valeurs par défaut ci-dessus en fallback mémoire sans lever d'exception fatale.

---

## 3. Spécification Chirurgicale par Fichier & Méthode (`TODO SOW Lot 1`)

### 3.1 `src/probes/amd_sysfs.py` (`AmdSysfsProbe`)
1. **`discover_sensors(self) -> CachedSensorPaths`** :
   - **Zéro indice codé en dur (`card0`, `hwmon0`)** :
     - Itérer `/sys/class/drm/card*/device` ; vérifier que `vendor` vaut `0x1002` (AMD).
     - Résoudre et mettre en cache dans `self._cache` (`CachedSensorPaths`) :
       - `vram_used` (`mem_info_vram_used`), `vram_total` (`mem_info_vram_total`)
       - `gtt_used` (`mem_info_gtt_used`), `gtt_total` (`mem_info_gtt_total`)
       - `gpu_busy` (`gpu_busy_percent`)
     - Sous `<card_device>/hwmon/hwmon*` :
       - `gpu_edge_temp` : chercher `temp*_label == "edge"` -> fichier `temp*_input` associé (sinon `temp1_input`).
       - `gfx_power` : chercher `power1_average` ou à défaut `power1_input`.
       - Attribut interne optionnel `gpu_clock` (`freq1_input` sous le hwmon `amdgpu`, divisé par `1_000_000` -> MHz).
     - Sous `/sys/class/hwmon/hwmon*` :
       - `soc_tctl_temp` : chercher `name` ∈ `{"k10temp", "zenpower"}` -> `temp1_input` (ou `temp*_label == "Tctl"`).
   - **Métadonnées silicium dynamiques (au démarrage, mises en cache)** :
     - `cpu_model` depuis `/proc/cpuinfo` (`model name`).
     - `gpu_model` et `gfx_arch` depuis `/sys/class/drm/card*/device/uevent` ou `/sys/class/kfd/kfd/topology/nodes/*/properties` (`gfx_target_version` ex: `110501` → `gfx1151`).
2. **`read_metrics(self) -> Dict[str, Any]`** :
   - Si `_cache` vide, appeler `discover_sensors()`.
   - Lire chaque `Path` résolu en `< 0.1 ms` (conversion octets → Go `/ (1024**3)`, millidegrés → `°C / 1000`, microwatts → `W / 1_000_000`).
   - **Fallback sécurisé (si sysfs absent ou illisible)** :
     - Vérifier les binaires via `shutil.which("sensors")` et `shutil.which("amdgpu_top")`.
     - Exécuter `subprocess.run(..., shell=False, timeout=2.0, capture_output=True, text=True)` (`sensors -j`, `amdgpu_top --dump -J`).
   - **CPU & RAM Système** :
     - Charge CPU globale via `/proc/stat` (delta entre deux lectures consécutives).
     - RAM OS / Libre via `/proc/meminfo` (`MemTotal`, `MemAvailable`).
   - **Batterie (`/sys/class/power_supply/BAT*`)** :
     - Lire `energy_now`, `energy_full`, `power_now`, `capacity`, `status`.
     - Si `BAT*` absent (station fixe) ou en charge (`power_now == 0`), retourner `battery_pct=100`, `battery_discharge_w=0`, `ai_runtime_remaining="AC"`, `idle_runtime_remaining="AC"`.
   - **Vecteurs 3-points & Historiques Sparkline (8 derniers points)** :
     - `history_temps_c` (max 8 entrées) + `temp_vector_3pt` avec hystérésis `±1.5 °C` (`↗`, `↘`, `→`) et pic max observé.
     - `history_watts` (max 8 entrées) + `power_vector_3pt` avec hystérésis `±2 W` et moyenne.
     - Seuils thermiques : `< 65°C` → `"OK"`, `65–84°C` → `"WARN"`, `>= 85°C` → `"CRIT"`.

---

### 3.2 `src/probes/engines_http.py` (`EnginesHttpProbe`)
Toutes les requêtes HTTP utilisent `urllib.request.urlopen(..., timeout=1.5)` encapsulé dans `asyncio.to_thread()` (`shell=False`, zéro lib HTTP tierce).

1. **`poll_vulkan_slots(self) -> Dict[str, Any]`** :
   - URL construite depuis `self.ports_config["services"]["vulkan"]` (`http://{host}:{port}{slots_endpoint}`) + `/metrics` et `lemonade_admin` (`/api/v1/health`).
   - Extraire :
     - `model` (via `/api/v1/health` ou `/v1/models` ou fallback cmdline).
     - `status_label` (`SLOT 0 · RUN` si `is_processing` ou `state == 1`, sinon `IDLE`), `speed_tps` (`timings.predicted_per_second`), `speed_pp_tps` (`timings.prompt_per_second`), `ttft_sec`.
     - `kv_used_tokens` (`n_past` + `n_decoded` ou `timings.cache_n`), `kv_max_tokens` (`n_ctx`), `kv_pct`.
     - `local_generated_tokens` / `local_served_tokens` depuis `/metrics` Prometheus (`llamacpp:tokens_predicted_total`, etc.).
   - **Inspection `/proc/<pid>/cmdline` et `/proc/<pid>/fdinfo/*`** (en Python pur) :
     - Identifier les processus `llama-server` / `lemonade-server` et `vllm`.
     - **Découverte dynamique du port `llama-server`** : si le port `config/ports.yaml` (`8001`) ne répond pas (par ex. chargement à la demande via Lemonade `:13305`), inspecter `http://{lemonade_admin.host}:{lemonade_admin.port}/api/v1/health` (`all_models_loaded` / `model_loaded`) et l'argument `--port` dans `/proc/<pid>/cmdline` de `llama-server` pour interroger `/slots` et `/metrics` sur le vrai port actif.
     - Extraire les flags CLI actifs (`-ngl`, `-ctk`, `-fa`, `--cache-reuse`, `-b`, `-ub`, `--temp`).
     - Sommer `drm-memory-vram:` et `drm-memory-gtt:` dans `/proc/<pid>/fdinfo/*` pour calculer l'empreinte réelle en Go par moteur (`vulkan_gb` vs `rocm_gb`).
   - *Dégradation gracieuse* : si connexion refusée / timeout ou aucun modèle chargé, retourner état `OFFLINE` / `IDLE` avec valeurs à `0` sans bloquer la boucle.

2. **`poll_vllm_metrics(self) -> Dict[str, Any]`** :
   - URL construite depuis `self.ports_config["services"]["vllm_rocm"]` (`http://{host}:{port}{metrics_endpoint}`).
   - Parser le texte Prometheus (`vllm:num_requests_running`, `vllm:num_requests_waiting`, `vllm:gpu_cache_usage_factor`, `vllm:gpu_prefix_cache_hit_rate`, `vllm:num_preemptions_total`, `vllm:time_to_first_token_seconds`, `vllm:generation_tokens_total`).
   - *Dégradation gracieuse* : si vLLM `:8000` est arrêté (cas normal en Topologie 1/2), retourner `running_reqs: 0`, `waiting_reqs: 0`, `gtt_pool_gb: 0.0`, `vllm_requests: []`, `status: "OFFLINE"`.

---

### 3.3 `src/probes/sqlite_readers.py` (`SqliteTelemetryReader`)
Toutes les connexions SQLite s'ouvrent impérativement avec `sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)`. En cas d'absence de fichier ou table/colonne manquante, ignorer proprement la source sans planter.

1. **`read_active_topology_and_tokens(self) -> Dict[str, Any]`** :
   - **Sources scannées (toutes optionnelles — ignorer proprement si fichier absent)** :
     - **Hermes Agent** : `~/.hermes/state.db` et `~/.hermes/profiles/*/state.db` :
       - Table `sessions` (`id`, `model`, `title`, `tool_call_count`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `last_activity_at`, `started_at`, `cwd`, `billing_provider`).
       - Table `session_model_usage` (`session_id`, `model`, `billing_provider`, `api_call_count`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `first_seen`, `last_seen`).
     - **OpenCode** : `~/.local/share/opencode/opencode.db` :
       - Table `session` (`id`, `directory`, `title`, `model`, `time_created`, `time_updated`, `tokens_input`, `tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write`).
       - Tables `message` / `part` (`session_id`, `time_created`, `time_updated`, `data` JSON).
     - **OpenWebUI** : `~/.local/share/open-webui/webui.db` (optionnel — absent si OpenWebUI utilise un autre répertoire de données ; dégrader proprement sans erreur). Si présent, tables `chat`, `user`.
   - **Arbitrage & Détection Topologie (1 à 4)** :
     - Comparer les timestamps `updated_at` les plus récents entre Hermes, OpenCode, OpenWebUI et les processus actifs (`engines_http`) pour déduire `active_topology` (1: Hermes→OpenCode→Proxy→Vulkan, 2: OpenCode Direct, 3: OpenWebUI→vLLM ROCm, 4: Hermes→Cloud).
   - **Calcul Glissant Cloud Quotas (60s stricte)** :
     - Filtrer les appels Cloud sur les 60 dernières secondes (`now - 60.0 <= ts`).
     - Calculer `tpm_sliding_60s`, `rpm_current`, `rpd_current` (sur 24h), `cache_hit_pct`, `cached_tokens`, `total_in_tokens`, `total_out_tokens`, `total_rsn_tokens`.
     - Statut selon `tpm_pct = round(tpm_sliding_60s / tpm_ceiling * 100)` :
       - `< 70%` → `status: "OK"`, `status_label: f"OK : QUOTA CLOUD NOMINAL ({pct}%)"`
       - `70–84%` → `status: "WARN"`, `status_label: f"WARN : RISQUE OUT OF QUOTA ({pct}%)"`
       - `>= 85%` → `status: "CRIT"`, `status_label: f"CRIT : OUT OF QUOTA IMMINENT ({pct}%)"`
   - **Fenêtres Temporelles (`time_windows`)** :
     - Pré-agréger les cumuls pour `SESSION`, `1H`, `24H`, `7J`, `TOTAL` afin de permettre le filtrage instantané côté UI.

---

### 3.4 `src/tokenwatcher_daemon.py` (`TokenWatcherDaemon`)
1. **`load_ports_config(self) -> Dict[str, Any]`** :
   - Charger `self.config_dir / "ports.yaml"` via `yaml.safe_load()` et initialiser `self.engines_probe = EnginesHttpProbe(config)`.
2. **`collect_once(self) -> Dict[str, Any]`** :
   - Exécuter de manière concurrente :
     - `self.sysfs_probe.read_metrics()` (via `asyncio.to_thread`)
     - `self.sqlite_reader.read_active_topology_and_tokens()` (via `asyncio.to_thread`)
     - `self.engines_probe.poll_vulkan_slots()` et `self.engines_probe.poll_vllm_metrics()` (`asyncio.gather`)
   - Calculer les champs dérivés :
     - `apu_pool_128g` (répartition totale APU entre `vulkan_gb`, `rocm_gb`, `os_gb`, `free_gb`).
     - `hardware_amd.efficiency_tok_per_j = round(tg_speed / gfx_power_w, 2)` (si `gfx_power_w > 0`).
   - Retourner un dictionnaire strictement conforme au **Contrat JSON V4.0.2** (Section 5 de `00_MASTER_SOW_V4_0_2.md`, incluant `time_windows`).
3. **`write_ipc_atomic(self, payload: Dict[str, Any]) -> None`** :
   - Lire le chemin cible depuis `ipc.state_file` (défaut `/tmp/tokenwatcher_widget_state.json`).
   - Écrire JSON dans `<state_file>.tmp` avec `flush()` puis appeler `os.replace(tmp_path, target_path)`.
4. **`run_forever(self) -> None`** :
   - Charger `ports_config`, découvrir les sondes au démarrage.
   - Boucle infinie à intervalle `ipc.polling_interval_sec` (1.0s) : `collect_once()` → `write_ipc_atomic()` ; capturer et journaliser les exceptions non-fatales pour garantir la continuité de service.
5. **Mode CLI `--once` dans `if __name__ == "__main__":`** :
   - Utiliser `argparse` pour supporter `--once` : exécute `load_ports_config()`, `payload = await daemon.collect_once()`, `daemon.write_ipc_atomic(payload)` et quitte proprement (`0`). Sans `--once`, exécute `daemon.run_forever()`.

---

## 4. Critères de Validation Lot 1
1. `./.venv/bin/python3 -m py_compile src/*.py src/probes/*.py` → code retour `0`.
2. Exécution de `./.venv/bin/python3 src/tokenwatcher_daemon.py --once` : `/tmp/tokenwatcher_widget_state.json` existe, est un JSON valide contenant `schema_version: "4.0.2"` et toutes les clés de premier niveau du contrat (`service_ports`, `runtime_config`, `session_hero`, `context_dna`, `apu_pool_128g`, `inference_engines`, `hardware_amd`, `cloud_quotas`, `time_windows`).
3. Vérification qu'aucun port ou chemin `/sys/class/drm/card0` n'est codé en dur et qu'aucun import tiers interdit n'est présent.
