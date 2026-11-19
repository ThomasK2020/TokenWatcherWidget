# Règles de Codage Agentique (Qwen3-Coder-30B-A3B · 64k Context)

1. **Lecture ciblée** : Inspecte d'abord les squelettes dans `src/` et les spécifications dans `SOW/specs/`.
2. **Édition chirurgicale** : Implémente chaque méthode marquée `raise NotImplementedError("TODO SOW...")`.
3. **Bibliothèque Standard + PyYAML uniquement** : N'importe aucun paquet externe (`aiohttp`, `httpx`, `requests`, `psutil` interdits). Utilise `urllib.request` avec `asyncio.to_thread()` pour les appels HTTP asynchrones non-bloquants, `sqlite3` en URI `?mode=ro`, et `yaml.safe_load()` pour `config/ports.yaml`.
4. **Validation systématique** : Après chaque lot, vérifie la syntaxe avec `python3 -m py_compile src/**/*.py` et valide le schéma JSON généré dans `/tmp/tokenwatcher_state.json`.
