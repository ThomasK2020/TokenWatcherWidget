# **1-1 (Principal) SOW Architecture TokenWatcher Widget and Data**

## **1\. Architecture de Récolte & Utilisation des Données (Fonctionnement Global)**

Le système **TokenWatcher** repose sur une séparation stricte entre **Acquisition (Noyau Unique)** et **Présentation (TopBar & Widget Visuel)** :

1. **Récolte par le Noyau Unique (tokenwatcher-daemon, Python 3, cycle 1.0s)** :  
* **Bases SQLite (Lecture seule URI mode=ro, zéro blocage des agents)** :  
  * **Hermes Agent** : scan dynamique de \~/.hermes/state.db et \~/.hermes/profiles/\*/state.db (tables sessions et session\_model\_usage).  
  * **OpenCode** : lecture de \~/.local/share/opencode/opencode.db (tables session et part, incluant les tokens input, output, reasoning, cache.read et cache.write).  
  * **OpenWebUI** : lecture de \~/.local/share/open-webui/webui.db (table chat, colonne JSON chat).  
* **API Locale REST** : requête HTTP GET http\://127.0.0.1:13305/api/v1/health (timeout=1.0s) vers **Lemonade Server** pour identifier le modèle .gguf chargé en VRAM.  
* **Sondes Matérielles AMD Strix Halo (sysfs / hwmon / /proc)** :  
  * Découverte dynamique au démarrage (zéro chemin cardX ou hwmonX codé en dur) via /sys/class/drm/card\*/device (vendor \== 0x1002) et /sys/class/hwmon/hwmon\* (amdgpu, k10temp/zenpower).  
  * Les objets Path résolus sont mis en cache mémoire (\_SENSOR\_CACHE) et lus en \< 0.1 ms à chaque cycle. En cas d absence, fallback sécurisé vers sensors \-j puis amdgpu\_top \--dump \-J (shell=False, timeout=2.0s).  
1. **Transport IPC Atomique (/tmp/tokenwatcher\_state.json)** :  
* Le démon écrit dans /tmp/tokenwatcher\_state.json.tmp puis exécute un remplacement atomique os.replace() vers /tmp/tokenwatcher\_state.json (stocké en RAM tmpfs). Aucune lecture partielle (JSON tronqué) n est possible côté Widget.  
1. **Consommation par le Widget (TokenWatcher-Widget, Python 3 \+ GTK 4.0)** :  
* Le Widget écoute /tmp/tokenwatcher\_state.json de façon asynchrone via Gio.FileMonitor (ou timer GLib.timeout\_add\_seconds(1)).  
* Pour les changements de temporalité (SESSION, 1H, 24H, 7J, TOTAL), le démon pré-agrège les fenêtres temporelles dans la clé time\_windows du JSON que le Widget filtre instantanément.

\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

## **2\. Tableau 1 — Fonctionnalités & Variables LLM / Multi-Agents (Référentiel V4.0.2)**

Ce tableau récapitule l'ensemble des sous-blocs, variables dynamiques (IPC JSON, runtimeConfig, servicePorts, i18n\_labels.json), sources d'acquisition backend et règles d'affichage de **TokenWatcher Next-Gen Widget Cockpit V4.0.2 MULTI-ENGINE** (intégrant le bloc **Inference Engines v3.4** en pleine largeur, les jauges à 10 carrés pastel et la règle d'inspection du Batch vLLM).

| Sous-Bloc / Composant UI (V4.0.2) | Variables & Clés Dynamiques (IPC JSON / runtimeConfig / i18n) | Source d'Acquisition Backend (tokenwatcher\_daemon.py) | Règles d'Affichage, Design & Valeur Ajoutée Utilisateur |
| :---- | :---- | :---- | :---- |
| **1\. En-tête Global & Fenêtres Temporelles** | • header.version\_badge (V4.0.2 MULTI-ENGINE) • header\_chain (Chaîne d'exécution active) • timeframe : \[SESSION\] \[1H\] \[24H\] \[7J\] \[TOTAL\] • view\_mode : Cockpit Complet \| Focus Silicium \| Pill HUD | • Arbitrage par horodatage updated\_at le plus récent entre state.db, opencode.db et webui.db. • Accumulateurs persistants en mémoire. | • **Pastille Live Pulsante** \+ chaîne active en temps réel. • Bascule instantanée FR/EN (applyLanguage() sans récursion via getRawString()) et protection stricte des balises feuilles DOM (el.children.length \=== 0). |
| **2\. Chaînes d'Exécution & Topologies (Pipeline 4-en-1)** | • activeTopology (1 à 4) • Ports dynamiques : servicePorts.{proxy, vulkan, vllm, webui, lemonadeAdmin} • Clés : pipeline.{hermes, opencode, proxy, vulkan, webui, vllm, cloud}.\* | • Lecture de config/ports.yaml au démarrage. • Inspection /proc/\<pid\>/cmdline. • Service lemonade-opencode-proxy.service (:13306 ➔ :8001). | • **4 topologies commutables** : 1\. *Hermes ➔ OpenCode ➔ Proxy ➔ Vulkan*, 2\. *OpenCode Direct*, 3\. *OpenWebUI ➔ vLLM ROCm*, 4\. *Hermes ➔ Cloud*. • **Design compact** : suppression du lien Inspecter ↗ et du tiroir inférieur redondant ; métriques clés intégrées directement dans chaque boîte. |
| **3\. Inférence Locale Active & Session Hero** | • inference.local.block\_title : ({engine} on {runtime} :{port}) • active\_model\_title, active\_model\_quant • active\_task\_title (ex: *SOW-01 ➔ Étape 3/5 : code\_patch\_file*) • agent\_mode (BUILD MODE · 22 tools) • directory (Chemin workspace cliquable) | • opencode.db (tables session et part). • \~/.hermes/state.db (tool\_calls). • /slots & /proc/\<pid\>/cmdline pour les flags actifs. | • Affiche dynamiquement le moteur et le serveur actifs (ex: VULKAN ENGINE on LLAMA-SERVER :8001 ou ROCm ENGINE on vLLM :8000). • Clic sur le chemin : ouvre directement le dossier projet dans Nautilus. |
| **4\. Anatomie du Contexte (Context DNA) & Détecteur Thought** | • ctx\_used / ctx\_max (54.2k / 65.5k — 83%) • ctx\_free (*Headroom libre* : 11.3k tok) • Décomposition : kv\_cached (48.7k, 94% Hit), prompt\_tokens (IN: \+3.1k), completion\_tokens (OUT: 1.8k), reasoning\_tokens (RSN: 640 tok, ratio 26%) • loop\_risk\_state (\[OK · StopTokens Actifs\]) | • Injection "stream\_options": {"include\_usage": true} par le Proxy :13306. • GET :8001/slots (n\_past, n\_decoded, timings.cache\_n). • Vérification des stop\_tokens (\</thought\>, \<channel\|\>). | • **Jauge à 10 Carrés Pastel (\#context-dna-squares-10)** remplaçant la barre continue : colorée par segment (Bleu \= KV Cache, Cyan \= Prompt In, Émeraude \= Out/Rsn). • Alerte anti-boucle infinie si le ratio RSN / OUT dérive. |
| **5\. Vitesses d'Inférence & Flags Fine-Tuning** | • speed\_tg (Débit génération : 58.4 t/s) • speed\_pp (Prefill : 1 420 t/s · Batch 512 / uBatch 128) • speed\_ttft (0.18 s · \--cache-reuse 256) • Badges : Temp 0.15, RepPen 1.0, KV q8\_0, FA: ON, NGL 999 | • timings.predicted\_per\_second (TG) et timings.prompt\_per\_second (PP) depuis /slots. • Extraction automatique des flags CLI du processus llama-server ou vllm. | • Permet à l'utilisateur de vérifier d'un coup d'œil l'effet de ses paramètres de fine-tuning (\-ctk q8\_0, \-fa on, \-b 512 \-ub 128, \--cache-reuse 256) sur le TTFT et le débit. |
| **6\. Pool Unifié APU 128 Go (Bloc Inference Engines v3.4)** | • hdr\_gen (14.8k), hdr\_serv (0.42M) • apu\_total\_ia (40.4 Go / 31.5%) • 4 segments : leg\_v\_gb (Vulkan 18.4 Go), leg\_r\_gb (ROCm 22.0 Go), leg\_os\_gb (OS 11.5 Go), leg\_free\_gb (Libre 76.1 Go) • Label : Zero-Copy LLM data via PCIe | • /proc/\<pid\>/fdinfo/\* (drm-memory-vram, drm-memory-gtt) par processus. • /proc/meminfo pour la RAM OS et libre. • Compteurs Prometheus tokens\_predicted\_total. | • **Empilement Vertical Pleine Largeur (100% width V4.0.2)** au-dessus du bloc Hardware. • **Grand Camembert Donut SVG 96px** \+ 4 cartes légendes alignées. • **Charte chromatique stricte** : zéro couleur verte sur l'OS (\#94A3B8) ou le Libre (\#263042) ; le vert est réservé au Heartbeat RUN. |
| **7\. Carte Moteur 1 : \[VULKAN\] (Lemonade / llama-server)** | • vulkan.engineBadge (\[VULKAN\]), vulkan.modelName, :8001 • Anneau SVG 80px : 14.4% / 18.4 Go (Alloc UMA · KV: 3.8 Go) • Capsule : Heartbeat dot \+ SLOT 0 · RUN \+ 58.4 t/s • engines.kv\_slot\_label : KV Slot ({ctx}) : • Client : OpenCode via Proxy :13306 | • GET http\://{host}:{port}/slots & /metrics (résolus via runtimeConfig.engines.vulkan et servicePorts.vulkan). | • **Jauge à 10 carrés Cyan Pastel (\#vulkan-squares-10)** \+ texte unifié 54.2k / 65.5k en gris ardoise \#94A3B8 et seul le pourcentage (83%) en gras coloré. • Ports en gris neutre (\#94A3B8) avec infobulle URL complète au survol ; noms OpenCode et Proxy en cyan. |
| **8\. Carte Moteur 2 : \[ROCm 7.x\] (vLLM \+ Inspecteur Batch)** | • rocm.engineBadge (\[ROCm 7.x\]), rocm.modelName, :8000 • Anneau SVG 80px : 17.2% / 22.0 Go (Pool GTT · 9.2G actif) • Capsule : BATCH ({req} req · {wait} wait) · 142.0 t/s ▼ • engines.paged\_cache\_label : Cache Contexte (Paged) : • Métriques Batch : Prefix Cache Hit: 78%, Preemptions: 0, TTFT moy: 0.24s • Liste nominative : REQ \#1..\#N (user, prompt, inTok, outTok, speed) | • **Métriques globales** : GET http\://{host}:{port}/metrics (vllm:num\_requests\_running, num\_requests\_waiting, gpu\_cache\_usage\_factor, gpu\_prefix\_cache\_hit\_rate, num\_preemptions\_total). • **Détail nominatif** : Jointure SQLite ?mode=ro sur \~/.local/share/open-webui/webui.db (tables chat, message, user) ou headers HTTP X-OpenWebUI-User-Name. | • **Terme vulgarisé** : Cache Contexte (Paged) : avec jauge à 10 carrés Rose Pastel (\#rocm-squares-10). • **Tiroir Batch déplié par défaut (is-open)**. • **Règle des ≤ 3 requêtes** : affiche max 3 requêtes par défaut ; si \> 3, affiche un bouton sobre gris ardoise (sans rouge ni soulignement) **Voir \+ (+{extra})** (ex: Voir \+ (+2)) / **Voir \-**. |
| **9\. Radar Quotas & Flux Cloud (Anti-HTTP 429\)** | • cloud.provider : {provider\_name} · {provider\_tier} • cloud.model : {model\_name} • cloud\_tpm\_60s (1.42M / 2.00M — 71%), cloud\_tpm\_free (580k tok/min) • cloud\_rpm (18 / 1 000), cloud\_rpd (482 / 10 000) • cachedContentTokenCount (Cache Cloud Hit: 60% · 110k tok) • Statuts : OK : QUOTA CLOUD NOMINAL, **WARN : RISQUE OUT OF QUOTA ({pct}%)**, CRIT : OUT OF QUOTA IMMINENT ({pct}%) | • Extraction locale zéro-polling depuis opencode.db (tokens.cache.read) et \~/.hermes/state.db (usageMetadata.cachedContentTokenCount / prompt\_tokens\_details.cached\_tokens). • Calcul de fenêtre glissante stricte sur 60 secondes. | • **Nouveau libellé explicite** : **WARN : RISQUE OUT OF QUOTA (71%)** remplaçant l'ancien RISQUE 429 MODÉRÉ. • **Jauge à 10 carrés (\#cloud-tpm-squares-10)** sur le TPM Glissant (60s). • Bouton d'action rapide **/compress** pour réduire immédiatement l'historique Hermes. |

\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

## **3\. Tableau 2 — Fonctionnalités & Capteurs Matériels (AMD Strix Halo / Système — V4.0.2)**

| Indicateur / Capteur Matériel | Variables & Clés Dynamiques (IPC JSON / i18n) | Source d'Acquisition Linux (amd\_sysfs.py — Zéro Hardcode) | Règles d'Affichage, Seuils & Valeur Ajoutée |
| :---- | :---- | :---- | :---- |
| **1\. En-tête Silicium & Profil Dynamique** | • hardware.silicon\_title : HARDWARE & AMD INDICATORS • hardware.silicon\_subtitle : {cpu\_model} · {gpu\_model} {gfx\_arch} · sondes sysfs/hwmon • Badge : Offload GPU: 100% | • Détection CPU via /proc/cpuinfo (Ryzen AI Max+ 395). • Détection GPU/Arch via /sys/class/drm/card\*/device/uevent & /dev/kfd (Radeon 8060S gfx1151). • Ratio couches GPU (NGL 999 ➔ 100%). | • **Zéro modèle matériel codé en dur** : s'adapte dynamiquement (*Strix Halo* gfx1151, *Gorgon Point* gfx1150, *Radeon PRO* gfx1100). • Confirme que 100 % des couches LLM sont exécutées sur l'iGPU/GPU sans repli CPU. |
| **2\. VRAM Dédiée (Carve-out BIOS)** | • hardware.memory.vram\_dedicated : VRAM Dédiée • vram\_used\_gb / vram\_total\_gb (13.5 / 16.0 Go — 84%) • vram\_free\_gb (Libre: 2.5 Go) | • /sys/class/drm/card\*/device/mem\_info\_vram\_used et mem\_info\_vram\_total (identifié par vendor \== 0x1002, mis en cache mémoire au démarrage). | • **Anneau radial SVG Cyan (\#06B6D4)** \+ micro-jauge \[■■■■□\]. • Sanctuarisé pour le GPU ; accueille en priorité le premier modèle chargé. |
| **3\. GTT UMA (Mémoire Unifiée Dynamique)** | • hardware.memory.gtt\_uma : GTT UMA • gtt\_used\_gb / gtt\_total\_gb (26.7 / 112.0 Go — 24%) • gtt\_free\_gb (Libre: 85.3 Go) | • /sys/class/drm/card\*/device/mem\_info\_gtt\_used et mem\_info\_gtt\_total. | • **Anneau radial SVG Violet (\#8B5CF6)** \+ micro-jauge \[■□□□□\]. • Permet le débordement transparent sans perte de vitesse (ex: second moteur vLLM ROCm ou modèle \> 16 Go) via la table IOMMU/GART (16 Go VRAM \+ 112 Go GTT \= 128 Go RAM). |
| **4\. Activité Compute GPU & CPU Zen 5** | • hardware.compute.activity : Compute • gpu\_busy\_percent (94%), gpu\_clock\_mhz (2 450 MHz) • cpu\_busy\_percent (CPU Zen5: 12%) | • gpu\_busy\_percent sous /sys/class/drm/card\*/device/. • freq1\_input (÷ 10⁶ pour MHz) sous hwmon\*. • /proc/stat pour la charge globale CPU. | • Micro-jauge \[■■■■■\] GPU Busy 94% (2 450 MHz). • Démontre l'efficacité de l'offload GPU (GPU à 94 % pendant que le CPU Zen 5 reste au repos à 12 %). |
| **5\. Sondes Thermiques Certifiées & Vecteur 3-Points** | • hardware.gpu\_edge : GPU T\* Edge (68°C \[WARN\]) • hardware.soc\_tctl : SoC hottest T\*Ctl (72°C) • hw\_vector\_temp : \[44° → 61° → 68°C ↗ Pic 74°C\] | • **GPU Edge** : /sys/class/drm/card\*/device/hwmon/hwmon\*/temp1\_input (temp1\_label \== "edge", ÷ 1 000). • **SoC Tctl** : /sys/class/hwmon/hwmon\*/temp1\_input (name \== "k10temp", ÷ 1 000). • Buffer circulaire avec hystérésis ± 1,5 °C. | • **Seuils** : \< 65°C \[OK\], 65–84°C \[WARN\] (Ambre), \>= 85°C \[CRIT\] (Rouge). • Courbe **Sparkline Canvas** lissée en arrière-plan du bloc Silicium. |
| **6\. Puissance Énergétique GFX & Efficacité** | • hardware.energy\_label : Énergie GFX (38 W) • hw\_vector\_power : \[12W → 48W → 38W ↘ Moy 31W\] • hardware.power.efficiency : Efficacité (1.53 tok/J) | • power1\_average ou power1\_input (÷ 10⁶ pour Watts) sous hwmon\* de la carte amdgpu. • **Calcul** : Efficacité \= TG Speed (t/s) ÷ Watts GFX. | • Vecteur 3-points avec hystérésis ± 2 W montrant la transition *Repos ➔ Prefill ➔ Génération*. • Mesure le rendement énergétique réel en tokens par Joule (tok/J). |
| **7\. Batterie & Autonomie IA Temps Réel** | • battery\_pct (74%), battery\_wh (66 Wh), battery\_discharge\_w (\-52 W) • hardware.battery.ai\_runtime : Autonomie IA (1h 18m vs Idle: 5h 40m) | • /sys/class/power\_supply/BAT0/ (energy\_now, energy\_full, power\_now). | • Calcule l'autonomie restante réelle sous charge d'inférence continue (energy\_now / power\_now) comparée à l'autonomie au repos. |

\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

## **4\. Tableau 3 — Contrôles Passifs d'Affichage, Temporalité, Vues & Intégration Hybride (V4.0.2)**

| Mécanisme / Contrôle UI | Variables & Clés (config/ & UI State) | Comportement Technique & Persistance | Règle Ergonomique & Architecture DOM (V4.0.2) |
| :---- | :---- | :---- | :---- |
| **1\. Empilement Vertical Pleine Largeur (V4.0.2 Layout)** | • Conteneur .grid.grid-cols-1.gap-4 (suppression de lg:grid-cols-2). | • Les sections \#section-slots (*Inference Engines v3.4*) et \#section-silicon (*Hardware & AMD Indicators*) occupent toujours **100 % de la largeur** du cockpit. | • Garantit une lisibilité maximale sur grand écran pour le Camembert APU, les jauges à 10 carrés, la liste des requêtes Batch vLLM et les vecteurs 3-points. |
| **2\. Sélecteur de Vues Adaptatives (3 Modes)** | • view.cockpit (Cockpit Complet) • view.silicium (Focus Silicium) • view.hud (Pill HUD) | • Masquage/Affichage dynamique sans rechargement DOM \+ recalcul automatique du Canvas Sparkline (setTimeout(drawSparkline, 50\)). | • **Cockpit Complet** : 6 sections. • **Focus Silicium** : isole uniquement *Inference Engines v3.4* \+ *Hardware & AMD Indicators* en pleine largeur. • **Pill HUD** : barre flottante ultra-compacte. |
| **3\. Tiroir Dynamique ⚙️ Ports & Config** | • config/ports.yaml : proxy (13306), vulkan (8001), vllm (8000), webui (8080), lemonadeAdmin (13305). • Sélecteurs de profils select-hw-profile & select-cloud-profile. | • Charge config/ports.yaml au démarrage du démon Python. • En mode UI, applyDynamicPorts() et applyDynamicHardwareAndCloud() mettent à jour instantanément tous les badges, URLs d'infobulles et chaînes. | • Permet de réassigner n'importe quel port en cas de conflit sur la workstation sans jamais toucher au code source HTML ou Python. |
| **4\. Moteur Multilingue FR / EN & Protection DOM** | • Fichier config/i18n\_labels.json (95 clés FR/EN). • Fonctions getRawString(key), getDynamicParams(), t(key, params) et applyLanguage(). | • Découplage strict entre getRawString() et getDynamicParams() pour empêcher toute récursion infinie (Maximum call stack size exceeded). | • **Règle d'or DOM** : data-i18n est posé exclusivement sur des balises feuilles \<span\> sans enfant HTML (if (el.children.length \> 0\) return;), protégeant les SVG, les badges et les chevrons. |
| **5\. Banc d'Essai & Simulation Interactive** | • simulateCodePatchStream() • simulateCloudBurst429() • cycleNextTopology() • toggleVllmReqCountDemo() (Sim: 5 req / 3 req) | • Bouton Pause/Play (1.0s) dans l'en-tête. • Permet de tester visuellement les seuils critiques (alert-critical-glow, WARN : RISQUE OUT OF QUOTA, règle Voir \+ (+2) sur vLLM) même hors charge. | • Sert à la fois d'outil de démonstration et de banc de validation visuelle lors du branchement du démon Python réel (Lot 2). |

\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

## **5\. Exemple de Payload JSON Consolidé (/tmp/tokenwatcher\_state.json — Contrat V4.0.2)**

Ce schéma JSON constitue le **contrat d'interface officiel** que le démon Python (`src/tokenwatcher_daemon.py`) écrit atomiquement chaque seconde dans `/tmp/tokenwatcher_state.json` et que le frontend (`ui/index.html` V4.0.2) consomme :

```json
{
  "schema_version": "4.0.2",
  "timestamp": 1763488400.125,
  "active_topology": 1,
  "timeframe": "SESSION",
  "service_ports": {
    "proxy": 13306,
    "vulkan": 8001,
    "vllm": 8000,
    "webui": 8080,
    "lemonade_admin": 13305
  },
  "runtime_config": {
    "cpu_model": "Ryzen AI Max+ 395",
    "gpu_model": "Radeon 8060S",
    "gfx_arch": "gfx1151",
    "backend": "vulkan",
    "runtime": "llama",
    "port": 8001,
    "provider_name": "Google AI Studio",
    "provider_tier": "Tier 1",
    "model_name": "GEMINI 4 ARGON"
  },
  "session_hero": {
    "active_model_title": "Qwen3-Coder-30B-A3B-Instruct-GGUF",
    "active_model_quant": "(Q4_K_M · 65 536 ctx · MoE 3B active)",
    "active_task_title": "SOW-01 GPU Telemetry ──► Étape 3/5 : code_patch_file",
    "agent_mode": "[BUILD MODE · 22 tools]",
    "workspace_dir": "/home/thomas/data/AI-workspace/TokenWatcherWidget/Widget",
    "tokens_total_session": 420800,
    "speed_tg_tps": 58.4,
    "speed_pp_tps": 1420,
    "ttft_sec": 0.18,
    "tuning_flags": ["Temp 0.15", "RepPen 1.0", "KV q8_0", "FA: ON", "stream_options: include_usage"]
  },
  "context_dna": {
    "ctx_used": 54200,
    "ctx_max": 65536,
    "ctx_pct": 83,
    "ctx_free": 11336,
    "kv_cached_tokens": 48700,
    "kv_hit_pct": 94.2,
    "prompt_in_delta": 3100,
    "prompt_in_total": 51800,
    "completion_out_tokens": 1800,
    "reasoning_tokens": 640,
    "reasoning_ratio_pct": 26,
    "loop_detector_status": "OK",
    "stop_tokens_active": true
  },
  "apu_pool_128g": {
    "total_gb": 128.0,
    "vulkan_gb": 18.4,
    "vulkan_pct": 14.4,
    "rocm_gb": 22.0,
    "rocm_pct": 17.2,
    "os_gb": 11.5,
    "os_pct": 9.0,
    "free_gb": 76.1,
    "free_pct": 59.5,
    "total_ai_gb": 40.4,
    "total_ai_pct": 31.5,
    "local_generated_tokens": 14800,
    "local_served_tokens": 420800
  },
  "inference_engines": {
    "vulkan": {
      "badge": "[VULKAN]",
      "model": "Qwen3-Coder-30B-A3B-GGUF",
      "host": "http://127.0.0.1",
      "port": 8001,
      "status_label": "SLOT 0 · RUN",
      "speed_tps": 58.4,
      "uma_alloc_gb": 18.4,
      "uma_alloc_pct": 14.4,
      "kv_alloc_gb": 3.8,
      "ctx_label": "64k",
      "kv_used_tokens": 54200,
      "kv_max_tokens": 65536,
      "kv_pct": 83,
      "flags": ["NGL 999", "KV q8_0", "FA:ON", "Reuse 256"],
      "client_name": "OpenCode",
      "proxy_name": "Proxy",
      "proxy_port": 13306
    },
    "rocm": {
      "badge": "[ROCm 7.x]",
      "model": "Qwen2.5-14B-Instruct-AWQ",
      "host": "http://127.0.0.1",
      "port": 8000,
      "running_reqs": 5,
      "waiting_reqs": 0,
      "speed_tps": 142.0,
      "gtt_pool_gb": 22.0,
      "gtt_pool_pct": 17.2,
      "gtt_active_gb": 9.2,
      "paged_cache_pct": 42,
      "prefix_cache_hit_pct": 78,
      "preemptions": 0,
      "avg_ttft_sec": 0.24,
      "flags": ["/dev/kfd gfx1151", "Continuous Batching", "AWQ 4-bit"],
      "client_name": "OpenWebUI",
      "client_port": 8080,
      "vllm_requests": [
        { "id": "REQ #1", "user": "Alice", "prompt": "Refactorisation FastMCP code_engine...", "in_tok": "6.4k", "out_tok": "820", "speed": "32.4 t/s" },
        { "id": "REQ #2", "user": "Bob", "prompt": "Diagnostic amdgpu GTT overflow...", "in_tok": "3.1k", "out_tok": "410", "speed": "31.5 t/s" },
        { "id": "REQ #3", "user": "Dev-CI", "prompt": "Tests unitaires SOW_02_WIDGET...", "in_tok": "8.9k", "out_tok": "1.2k", "speed": "28.3 t/s" },
        { "id": "REQ #4", "user": "Chloé", "prompt": "Audit sécurité OAuth2 proxy :13306...", "in_tok": "4.7k", "out_tok": "650", "speed": "25.8 t/s" },
        { "id": "REQ #5", "user": "Studio-DJ", "prompt": "Génération shaders Vulkan compute...", "in_tok": "5.2k", "out_tok": "390", "speed": "24.0 t/s" }
      ]
    }
  },
  "hardware_amd": {
    "offload_gpu_pct": 100,
    "vram_used_gb": 13.5,
    "vram_total_gb": 16.0,
    "vram_pct": 84,
    "vram_free_gb": 2.5,
    "gtt_used_gb": 26.7,
    "gtt_total_gb": 112.0,
    "gtt_pct": 24,
    "gtt_free_gb": 85.3,
    "gpu_busy_pct": 94,
    "gpu_clock_mhz": 2450,
    "cpu_busy_pct": 12,
    "gpu_edge_temp_c": 68,
    "gpu_temp_status": "WARN",
    "soc_tctl_temp_c": 72,
    "temp_vector_3pt": "[44° → 61° → 68°C ↗ Pic 74°C]",
    "history_temps_c": [44, 48, 52, 59, 61, 65, 68, 68],
    "gfx_power_w": 38,
    "power_vector_3pt": "[12W → 48W → 38W ↘ Moy 31W]",
    "history_watts": [12, 18, 28, 48, 45, 42, 38, 38],
    "efficiency_tok_per_j": 1.53,
    "battery_pct": 74,
    "battery_wh": 66,
    "battery_discharge_w": -52,
    "ai_runtime_remaining": "1h 18m",
    "idle_runtime_remaining": "5h 40m"
  },
  "cloud_quotas": {
    "status": "WARN",
    "status_label": "WARN : RISQUE OUT OF QUOTA (71%)",
    "tpm_sliding_60s": 1420000,
    "tpm_ceiling": 2000000,
    "tpm_pct": 71,
    "tpm_headroom": 580000,
    "rpm_current": 18,
    "rpm_ceiling": 1000,
    "rpd_current": 482,
    "rpd_ceiling": 10000,
    "cache_hit_pct": 60,
    "cached_tokens": 110000,
    "total_in_tokens": 184000,
    "total_out_tokens": 6400,
    "total_rsn_tokens": 2100
  }
}
```

\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_

## **6\. Design Visuel & Modularité du Widget (Flat Design, Sans Icône, Color-Coded)**

* 

### **6.1 Maquette Structurelle du Widget**

La maquette Tokenwatcher\_next\_gen\_widget\_cockpit\_v4\_0\_2 du widget est dans le repo Github ou en local et porte le nom de fichier suivant: 

ui/index.html

