# Spécification Technique — SOW 02 : Interface Frontend GTK4/WebKit, IPC & Système i18n

## 1. Vue d'Ensemble & Périmètre
Ce document constitue le cahier des charges exécutable (SOW Lot 2) pour le branchement IPC réel du frontend TokenWatcher Widget V4.0.2 MULTI-ENGINE :
- `ui/index.html` : Cockpit HTML5/Tailwind CSS/JS (Flat Design, color-coded, DOM protégé).
- `src/widget_runner.py` : Exécuteur d'application GTK 4.0 avec WebKitGTK 6.0 (`allow_file_access_from_file_urls` et `allow_universal_access_from_file_urls` activés).
- `config/i18n_labels.json` : Dictionnaire de traduction FR/EN (95+ clés).
- `config/ports.yaml` : Configuration des ports dynamiques des services.

---

## 2. Intégration GTK4 / WebKitGTK (`src/widget_runner.py`)
- Fenêtre GTK4 (`Gtk.ApplicationWindow`, taille par défaut `980x920`).
- Typelibs `Gtk-4.0` et `WebKit-6.0` chargés avec injection automatique de `~/.local/lib/girepository-1.0` dans `GI_TYPELIB_PATH`.
- `WebKit.Settings`:
  - `set_allow_file_access_from_file_urls(True)`
  - `set_allow_universal_access_from_file_urls(True)`
  Ces deux réglages autorisent `ui/index.html` (chargé via `file:///.../ui/index.html`) à lire directement `/tmp/tokenwatcher_state.json` et `../config/i18n_labels.json` via `fetch('file:///...')`.

---

## 3. Pont IPC Réel (`file:///tmp/tokenwatcher_state.json`) & Bascule Live/Simulation

### 3.1 Poller IPC Asynchrone dans `ui/index.html`
> **Piège critique WebKitGTK 6.0 (`file://`)** : Dans `WebKit.WebView`, un `fetch('file:///...')` réussi renvoie **`resp.status === 0` et `resp.ok === false`**. Il ne faut JAMAIS écrire `if (!resp.ok) throw ...` sans autoriser `resp.status === 0` (`if (!resp.ok && resp.status !== 0)`), sinon 100 % des lectures IPC et `i18n_labels.json` échoueront silencieusement !

Remplacer la boucle de simulation pure (lignes `2106–2126`) par un poller dual **Live IPC / Fallback Simulation** toutes les `1000 ms` (`simulationRunning` piloté par `toggleSimulation()`) :
```javascript
let ipcLiveConnected = false;

async function pollIpcState() {
  if (!simulationRunning) return;
  try {
    const resp = await fetch(`file:///tmp/tokenwatcher_state.json?t=${Date.now()}`, { cache: 'no-store' });
    if (!resp.ok && resp.status !== 0) throw new Error(`HTTP ${resp.status}`);
    const payload = await resp.json();
    if (payload && payload.schema_version === '4.0.2') {
      ipcLiveConnected = true;
      applyIpcPayload(payload);
      return;
    }
  } catch (err) {
    ipcLiveConnected = false;
  }
  // Fallback simulation uniquement si le démon IPC n'est pas encore démarré
  stepFallbackSimulation();
}
```
- **Règle de continuité** : En mode Live IPC (`ipcLiveConnected === true`), les boutons du banc d'essai (`simulateCodePatchStream`, `simulateCloudBurst429`) peuvent déclencher un override temporaire de démonstration (ou basculer en mode test) sans écraser silencieusement les métriques réelles au cycle suivant.

### 3.2 Carte de Lecture Ciblée `ui/index.html` (Protection Fenêtre 64k)
Ne pas charger tout `ui/index.html` inutilement en contexte : cibler ces plages exactes :
- **HTML — Éléments sans `id` sur valeur dynamique** (poser un `<span id="...">` uniquement sur la valeur numérique, sans jamais mettre `data-i18n` sur un parent avec enfants) :
  - Ligne `890` (`#vram-free-val`), ligne `905` (`#gtt-free-val`), ligne `923` (`#hw-cpu-busy`), ligne `952` (`#hw-efficiency`), ligne `963` (`#hw-ai-runtime`, `#hw-idle-runtime`), lignes `1029`/`1039`/`1042` (`#cloud-rpd-val`, `#cloud-cache-hit-val`, `#cloud-cumul-val`).
- **JS principal (`<script>` lignes `1072–2226`)** :
  - `1074–1307` : `const DEFAULT_I18N` (à conserver comme `const` et enrichir par mutation de ses clés via `Object.assign`, **jamais** par réassignation `DEFAULT_I18N = ...`).
  - `1310–1458` : `runtimeConfig`, `ALL_VLLM_REQUESTS`, `servicePorts`, `state`, `getRawString()`, `getDynamicParams()`, `t()`, `applyLanguage()`.
  - `1651–1731` : `updateApuDonutAndCards()` (à paramétrer pour consommer `apu_pool_128g` et `inference_engines` réels).
  - `2028–2223` : `updateUI()`, `setInterval()`, `window.onload`.

### 3.2 Contrat de Mapping Exact JSON IPC → DOM / Variables JS (`applyIpcPayload(data)`)
Chaque section du JSON `/tmp/tokenwatcher_state.json` (Section 5 de `00_MASTER_SOW_V4_0_2.md`) doit hydrater les objets JS et IDs DOM existants :

1. **`service_ports` & `runtime_config`** :
   - Synchroniser `servicePorts.{proxy, vulkan, vllm, webui, lemonadeAdmin}` depuis `data.service_ports` (attention au renommage `lemonade_admin` → `lemonadeAdmin`) et mettre à jour les champs du tiroir `#ports-config-modal`.
   - Synchroniser `runtimeConfig.{cpu_model, gpu_model, gfx_arch, backend, runtime, port, provider_name, provider_tier, model_name}`.
   - Si `data.active_topology` change, appeler `setTopology(data.active_topology, { silent: true })`.
2. **`session_hero` & `context_dna`** :
   - `#active-model-title`, `#active-model-quant`, `#active-task-title`, `#active-agent-mode`, `#session-tokens-display`, `#speed-tg`, `#speed-pp`, `#speed-ttft`, `#hero-tuning-badges`.
   - `state.ctxUsed = data.context_dna.ctx_used`, `state.ctxMax = data.context_dna.ctx_max` (supprimer le `/ 65.5k` codé en dur dans `updateUI()`, utiliser `(state.ctxMax / 1000).toFixed(1) + 'k'`).
   - `#val-kv-hit`, `#val-in`, `#val-out`, `#val-rsn`, `#loop-risk-label`, `#context-dna-squares-10`.
   - Timeframe (`SESSION`, `1H`, `24H`, `7J`, `TOTAL`) : lire les totaux pré-agrégés dans `data.time_windows[state.timeframe]` au lieu d'appliquer des multiplicateurs fictifs lorsque `ipcLiveConnected === true`.
3. **`apu_pool_128g` & `inference_engines`** :
   - Paramétrer `updateApuDonutAndCards(data.apu_pool_128g, data.inference_engines)` pour utiliser les valeurs réelles (`total_gb`, `vulkan_gb`, `rocm_gb`, `os_gb`, `free_gb`, `total_ai_gb`, `local_generated_tokens`, `local_served_tokens`) au lieu des constantes `18.4 / 22.0 / 11.5`.
   - Hydrater la carte **Vulkan** : `#vulkan-engine-badge`, `#vulkan-model-title`, `#vulkan-port-badge`, `#vulkan-slot-label`, `#vulkan-speed`, `#ring-vulkan`, `#ring-vulkan-pct`, `#vulkan-mem-gb`, `#vulkan-kv-gb`, `#vulkan-kv-label`, `#vulkan-squares-10`, `#vulkan-kv-tokens`, `#vulkan-kv-pct`, `#vulkan-flags-container`, `#vulkan-client-name`, `#vulkan-proxy-port`.
   - Hydrater la carte **ROCm vLLM** : `#rocm-engine-badge`, `#rocm-model-title`, `#rocm-port-badge`, `#rocm-batch-pill-text`, `#rocm-speed`, `#ring-rocm`, `#ring-rocm-pct`, `#rocm-mem-gb`, `#rocm-active-val`, `#rocm-squares-10`, `#rocm-paged-used-total`, `#rocm-paged-pct`, `#vllm-drawer-meta-label`, et remplacer `ALL_VLLM_REQUESTS` par `data.inference_engines.rocm.vllm_requests` puis appeler `renderVllmRequestsList()`.
4. **`hardware_amd`** :
   - `#vram-pct`, `#vram-val`, `#gauge-vram-circle` + headroom libre ; `#gtt-pct`, `#gtt-val`, `#gauge-gtt-circle` + headroom libre.
   - `#hw-gpu-busy`, `#hw-gpu-clock`, CPU Zen5 %, `#hw-temp-edge`, `#hw-vector-temp`, `#hw-temp-soc`, `#hw-power-watts`, `#hw-vector-power`, efficacité `tok/J`, `#hw-battery`, autonomie IA vs Idle.
   - `state.historyTemps = data.hardware_amd.history_temps_c`, `state.historyWatts = data.hardware_amd.history_watts` → `drawSparkline()`.
5. **`cloud_quotas`** :
   - `#cloud-tpm-text` (`tpm_sliding_60s / tpm_ceiling`), `#cloud-tpm-pct`, `#cloud-tpm-squares-10`, `#cloud-tpm-free`, `#cloud-rpm-val`, RPD, Cache Hit %, Cumul Cloud, `#alert-429-badge`, `#alert-429-text`.

---

## 4. Architecture DOM, Ergonomie & Règle d'Or i18n (`ui/index.html` V4.0.2)

### 4.1 Protection Stricte du Layout & des Feuilles DOM
- **Empilement Vertical Pleine Largeur** : Conserver `.grid.grid-cols-1.gap-4` sur `#section-slots` et `#section-silicon` (jamais de `lg:grid-cols-2`).
- **Chargement i18n dynamique (sans réassigner `const DEFAULT_I18N`)** :
  ```javascript
  async function loadExternalI18n() {
    try {
      const resp = await fetch(`../config/i18n_labels.json?t=${Date.now()}`, { cache: 'no-store' });
      if (!resp.ok && resp.status !== 0) return;
      const loaded = await resp.json();
      if (loaded && loaded.fr) Object.assign(DEFAULT_I18N.fr, loaded.fr);
      if (loaded && loaded.en) Object.assign(DEFAULT_I18N.en, loaded.en);
      applyLanguage();
    } catch (_) { /* Fallback synchrone sur DEFAULT_I18N */ }
  }
  ```
- **Découplage sans récursion** : Conserver `getRawString(key)`, `getDynamicParams()`, `t(key, params)` et `applyLanguage()`.
- **Règle d'or DOM (`el.children.length === 0`)** :
  ```javascript
  if (el.id === 'pipeline-nodes-chain' || el.classList.contains('pipeline-row')) return;
  if (el.children.length > 0) return;
  ```
  Toute nouvelle balise dynamique (ex: VRAM libre, RPD, efficacité) ajoutée pour recevoir des valeurs IPC doit avoir un `id` propre sans écraser un élément parent contenant un `<span data-i18n="...">`.

### 4.2 Règle d'Affichage Batch vLLM (≤ 3 requêtes)
- Tiroir `#vllm-batch-drawer` déplié par défaut (`is-open`).
- `renderVllmRequestsList()` affiche **au maximum 3 requêtes** par défaut ; si `requests.length > 3`, affiche `#vllm-requests-more-wrapper` avec le bouton sobre gris ardoise (`Voir + (+N)` / `Voir -`).
- Si `vllm_requests` est vide (`0 req`), afficher un état neutre `Aucune requête active sur vLLM`.

### 4.3 Sélecteur de Vues Adaptatives (3 Modes)
- `setViewMode('cockpit')` : Cockpit Complet (6 sections).
- `setViewMode('silicium')` : Focus Silicium (`#section-slots` + `#section-silicon` uniquement) + `setTimeout(drawSparkline, 50)`.
- `setViewMode('hud')` : Pill HUD (`#hud-pill-container` alimenté en temps réel par `#pill-chain-label`, `#pill-model-name`, `#pill-tokens`, `#pill-kv`, `#pill-tps`, `#pill-temp`).

---

## 5. Critères de Validation Lot 2
1. Vérifier que tous les `[data-i18n]` dans `ui/index.html` restent des balises feuilles (`el.children.length === 0`) et qu'aucun `lg:grid-cols-2` n'est introduit.
2. Vérifier que les appels `fetch('file://...')` / `fetch('../config/i18n_labels.json')` testent `if (!resp.ok && resp.status !== 0)` et fusionnent via `Object.assign(DEFAULT_I18N.fr, ...)`.
3. Générer `/tmp/tokenwatcher_state.json` via `./.venv/bin/python3 src/tokenwatcher_daemon.py --once` puis lancer `timeout 3s ./.venv/bin/python3 src/widget_runner.py` : un code de sortie `124` (arrêt propre par `timeout` après 3s d'affichage sans exception Python/WebKit) confirme la validité GTK4 + WebKitGTK 6.0.
4. Tester la bascule FR/EN (`toggleLanguage()`), les 3 vues (`cockpit`, `silicium`, `hud`) et le tiroir vLLM (`Voir +` / `Voir -`).
