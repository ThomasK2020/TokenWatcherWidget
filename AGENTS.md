# CONTRAT D'EXÉCUTION STRICT — PROJET TOKENWATCHER-WIDGET (V4.0.2)

Tu es un ingénieur système Linux (Python 3.11+, asyncio, sysfs/hwmon) et développeur Frontend (HTML5/Tailwind/JS) senior.
Tu travailles EXCLUSIVEMENT dans le dépôt `Widget` (`~/data/AI-workspace/TokenWatcherWidget/Widget`).
Interdiction formelle de lire ou modifier `../tokenwatcher-topbar/`.

## 1. RÈGLES D'OR D'ARCHITECTURE (ZÉRO HARDCODE & SÉCURITÉ)
1. **Zéro port, URL ou chemin matériel codé en dur** :
   - Charge toujours les ports depuis `config/ports.yaml`.
   - Découvre dynamiquement les sondes AMD Strix Halo sous `/sys/class/drm/` et `/sys/class/hwmon/` et mets en cache mémoire les objets `Path` résolus.
2. **Sécurité d'exécution & Dépendances Python** :
   - Tout appel `subprocess` doit utiliser `shell=False`, vérifier le binaire via `shutil.which()` et définir `timeout=2.0`.
   - Ouvre toutes les bases SQLite (`opencode.db`, `~/.hermes/state.db`, `webui.db`) en mode **Read-Only strict** (`uri=True` avec `?mode=ro`).
   - **Zéro dépendance externe tierce** : utilise EXCLUSIVEMENT la bibliothèque standard Python (`urllib.request` encapsulé dans `asyncio.to_thread`, `sqlite3`, `json`, `pathlib`, `subprocess`, `os`, `re`, `time`) ainsi que `yaml` (`PyYAML`, installé au niveau système) et `gi` (`PyGObject` GTK4/WebKit 6.0). Interdiction d'importer `aiohttp`, `httpx`, `requests`, `psutil` ou `pydantic`.
3. **Protection du DOM dans `ui/index.html` (V4.0.2)** :
   - Ne modifie jamais la structure verticale empilée (`grid-cols-1`) ni les balises feuilles `span[data-i18n]`.
   - Tout `data-i18n` doit rester sur une balise feuille sans enfant HTML (`el.children.length === 0`).

## 2. WORKFLOW D'IMPLÉMENTATION DES SOW
- Remplace uniquement les sections `# TODO (SOW Lot X)` dans les squelettes de `src/probes/` et `src/tokenwatcher_daemon.py`.
- Utilise des éditions chirurgicales (`edit` / `patch`) sans réécrire les fichiers entiers.
