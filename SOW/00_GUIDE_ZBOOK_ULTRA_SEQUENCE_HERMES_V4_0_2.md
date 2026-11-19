# 🚀 SOW & TokenWatcher Coding Pipeline — Guide Opérationnel ZBook Ultra & Séquence Hermes (V4.0.2 MULTI-ENGINE)

- **Référence Google Doc** : `1lzdkJKdfQEazKRjZFV78VuoxF3GzyMAw` (*`(start here) Guide ZBook Ultra & Séquence de Commandes Hermes (V4.0.2).md`*)
- **Version Cible** : TokenWatcher Next-Gen Widget Cockpit V4.0.2 MULTI-ENGINE (Option B — Squelettes Typés)
- **Machine Cible** : HP ZBook Ultra (AMD Ryzen AI Max+ 395 · Radeon 8060S gfx1151 · 128 Go UMA)
- **Environnement Python Hybride** : `~/data/AI-workspace/TokenWatcherWidget/Widget/.venv` (`--system-site-packages` : `gi` GTK4/WebKit 6.0 + `PyYAML` + Stdlib Python)
- **Typelibs GI GTK4 / WebKit 6.0** : `~/.local/lib/girepository-1.0` (`WebKit-6.0.typelib`, `JavaScriptCore-6.0.typelib`)
- **Chaîne d'Exécution** : `Hermes CLI ──► OpenCode CLI ──► Proxy (:13306) ──► Lemonade Vulkan (:8001) [Qwen3-Coder-30B-A3B · 64k ctx]`

---

## PARTIE 1 : Préparation & Vérification de la Stack Locale (TERMINÉE & VALIDÉE)

### Étape 1.3 — Vérifier la Stack Locale & Ouvrir le Cockpit GTK4 V4.0.2
```bash
# 1. Vérifier le proxy OpenCode (:13306) et Lemonade Vulkan (:8001)
curl -s http://127.0.0.1:8001/slots | head -c 200 && echo -e "\n✅ Vulkan :8001 OK"
systemctl --user is-active lemonade-opencode-proxy.service && echo "✅ Proxy :13306 OK"

# 2. Tester l'ouverture visuelle immédiate du prototype V4.0.2 en mode natif GTK4 :
~/data/AI-workspace/TokenWatcherWidget/Widget/.venv/bin/python3 ~/data/AI-workspace/TokenWatcherWidget/Widget/src/widget_runner.py &
```

---

## PARTIE 2 : Enchaînement des Commandes & Requêtes pour Hermes (Étape par Étape)

### 🔹 ÉTAPE 1 (Dans Hermes) : Audit de l'Arborescence, Découpage des 3 Sous-SOWs & Premier Commit
```text
Nous travaillons exclusivement dans ~/data/AI-workspace/TokenWatcherWidget/Widget (ne touche jamais à ../tokenwatcher-topbar).
Exécute les actions suivantes :
1. Vérifie la présence de AGENTS.md, opencode.json, config/ports.yaml, config/i18n_labels.json, ui/index.html et SOW/specs/00_MASTER_SOW_V4_0_2.md.
2. À partir du référentiel maître SOW/specs/00_MASTER_SOW_V4_0_2.md (Tableaux 1, 2, 3 et Section 5 Payload JSON), vérifie/génère les 3 fichiers de spécifications détaillés suivants dans SOW/specs/ :
   - SOW/specs/01_BACKEND_TELEMETRY_DAEMON.md (Lot 1 : implémentation de src/probes/amd_sysfs.py, src/probes/engines_http.py, src/probes/sqlite_readers.py et src/tokenwatcher_daemon.py en Stdlib + PyYAML uniquement, écrivant le JSON exact de la Section 5 dans /tmp/tokenwatcher_widget_state.json).
   - SOW/specs/02_FRONTEND_IPC_AND_I18N.md (Lot 2 : branchement de ui/index.html V4.0.2 sur file:///tmp/tokenwatcher_widget_state.json?t=Date.now() avec cache: 'no-store' et ../config/i18n_labels.json sous WebKitGTK 6.0 en préservant strictement l'empilement vertical grid-cols-1, les jauges à 10 carrés, la règle <= 3 requêtes vLLM avec bouton Voir + (+N) et les balises feuilles span[data-i18n]).
   - SOW/specs/03_SYSTEMD_AND_DESKTOP_PACKAGING.md (Lot 3 : services systemd utilisateur tokenwatcher-daemon.service et tokenwatcher-widget.service utilisant .venv/bin/python3 et GI_TYPELIB_PATH=%h/.local/lib/girepository-1.0 + lanceur desktop).
3. Vérifie que SOW/specs/00_MASTER_SOW_V4_0_2.md est présent dans le tableau "instructions" de opencode.json.
4. Effectue le commit sur la branche main et affiche git status.
```

### 🔹 ÉTAPE 2 (Dans Hermes) : Exécution du Lot 1 par OpenCode (Sondes AMD Strix Halo, HTTP `/slots` & `/metrics`, SQLite Read-Only)
```text
Lance l'implémentation du Lot 1 via OpenCode en exécutant :
~/data/AI-workspace/TokenWatcherWidget/Widget/scripts/run_sow_widget.sh SOW/specs/01_BACKEND_TELEMETRY_DAEMON.md

Une fois qu'OpenCode a terminé :
1. Vérifie avec l'environnement hybride (.venv --system-site-packages, Stdlib + PyYAML uniquement) que `./.venv/bin/python3 -m py_compile src/tokenwatcher_daemon.py src/probes/*.py` passe sans aucune erreur.
2. Lance un test unique de collecte (`./.venv/bin/python3 src/tokenwatcher_daemon.py --once`) pour générer `/tmp/tokenwatcher_widget_state.json`.
3. Vérifie que `/tmp/tokenwatcher_widget_state.json` est valide et contient bien les clés `schema_version` (`"4.0.2"`), `runtime_config`, `service_ports`, `apu_pool_128g`, `inference_engines` (`vulkan` et `rocm`), `hardware_amd` (`gpu_edge_temp_c`, `soc_tctl_temp_c`, `vram_used_gb`, `gtt_used_gb`), `cloud_quotas` (`status` dans `OK|WARN|CRIT` et `status_label`) et `time_windows`.
4. Si tout est conforme, fais un `git commit -m "feat(backend): implement Lot 1 telemetry daemon and probes (V4.0.2)"`.
```

### 🔹 ÉTAPE 3 (Dans Hermes) : Exécution du Lot 2 par OpenCode (Raccordement de `ui/index.html` V4.0.2 au flux réel `/tmp/tokenwatcher_widget_state.json`)
```text
Lance l'implémentation du Lot 2 via OpenCode en exécutant :
~/data/AI-workspace/TokenWatcherWidget/Widget/scripts/run_sow_widget.sh SOW/specs/02_FRONTEND_IPC_AND_I18N.md

Consignes de contrôle strict après l'exécution d'OpenCode :
1. Vérifie dans ui/index.html que la boucle de polling lit désormais /tmp/tokenwatcher_widget_state.json toutes les 1.0s avec cache-busting WebKitGTK 6.0 (`fetch('file:///tmp/tokenwatcher_widget_state.json?t=' + Date.now(), { cache: 'no-store' })` et test `if (!resp.ok && resp.status !== 0)`) et fusionne `../config/i18n_labels.json` dans `DEFAULT_I18N` via `Object.assign` (avec fallback propre sur les données de démonstration si le démon est en pause ou absent).
2. Vérifie que le conteneur des sections 4 et 5 conserve la classe "grid grid-cols-1 gap-4" (aucun lg:grid-cols-2) pour maintenir l'empilement vertical 100% largeur.
3. Vérifie qu'aucune balise parente avec enfants HTML n'a reçu d'attribut data-i18n et que getRawString() est toujours utilisé dans getDynamicParams().
4. Valide l'ouverture GTK4 + WebKitGTK 6.0 sans erreur (`timeout 3s ./.venv/bin/python3 src/widget_runner.py` : code retour `124` = succès GUI après 3s sans crash).
5. Fais un git commit -m "feat(ui): connect V4.0.2 cockpit to live IPC state and external i18n_labels.json".
```

### 🔹 ÉTAPE 4 (Dans Hermes) : Exécution du Lot 3 par OpenCode (Services Systemd Utilisateur & Lanceur Desktop GTK4)
```text
Lance l'implémentation du Lot 3 via OpenCode en exécutant :
~/data/AI-workspace/TokenWatcherWidget/Widget/scripts/run_sow_widget.sh SOW/specs/03_SYSTEMD_AND_DESKTOP_PACKAGING.md

Après exécution :
1. Vérifie les fichiers dans systemd/tokenwatcher-daemon.service et systemd/tokenwatcher-widget.service (utilisant ExecStart=/home/thomas/data/AI-workspace/TokenWatcherWidget/Widget/.venv/bin/python3 et Environment=GI_TYPELIB_PATH=/home/thomas/.local/lib/girepository-1.0 pour GTK4/WebKit 6.0) ainsi que le script d'installation/lancement.
2. Active et démarre le démon collecteur en mode utilisateur (systemctl --user daemon-reload && systemctl --user enable --now tokenwatcher-daemon.service).
3. Vérifie avec systemctl --user status tokenwatcher-daemon.service et ls -la /tmp/tokenwatcher_widget_state.json que le fichier IPC est bien rafraîchi chaque seconde.
4. Archive les SOWs terminés dans SOW/archive/, fais un git commit -m "feat(packaging): add systemd user services and GTK4 desktop launcher" et pousse sur origin main (git push -u origin main).
```
