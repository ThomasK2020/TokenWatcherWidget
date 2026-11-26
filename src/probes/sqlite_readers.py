"""
Sonde SQLite Read-Only : OpenCode, Hermes Agent & OpenWebUI (V4.2.0).
Ouverture stricte en lecture seule (?mode=ro) via sqlite3.
- Scanne ~/.hermes/state.db et ~/.hermes/profiles/*/state.db (hors backups/snapshots)
- Variabilisation du profil actif Hermes (hermes.active_profile / include_all_profiles)
- Ventilation multi-sessions et quotas Cloud par modèle (Argon, Hydrogen, Flash, etc.)
- Scanne ~/.local/share/opencode/opencode.db (multi-sessions & sous-agents parent_id)
- Scanne ~/.local/share/open-webui/webui.db (optionnel, dégradation gracieuse)
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_ROUTING_CFG: Dict[str, List[str]] = {
    "cloud_hosts": [
        "googleapis.com",
        "generativelanguage.googleapis.com",
        "aiplatform.googleapis.com",
        "api.openai.com",
        "api.anthropic.com",
        "openrouter.ai",
        "githubcopilot.com",
        "api.githubcopilot.com",
        "copilot-proxy.githubusercontent.com",
        "api.mistral.ai",
        "api.groq.com",
        "api.deepseek.com",
        "api.x.ai",
        "api.together.xyz",
        "api.perplexity.ai",
        "api.fireworks.ai",
        "openai.azure.com",
    ],
    "local_hosts": ["localhost", "127.0.0.1", "::1", "0.0.0.0"],
    "cloud_providers": [
        "gemini",
        "google",
        "vertex",
        "openai",
        "codex",
        "copilot",
        "github-copilot",
        "anthropic",
        "claude",
        "openrouter",
        "azure",
        "bedrock",
        "groq",
        "mistral",
        "deepseek",
        "xai",
        "together",
        "perplexity",
        "fireworks",
    ],
    "local_providers": [
        "local",
        "lemonade",
        "llamacpp",
        "llama-cpp",
        "vllm",
        "ollama",
        "lmstudio",
        "jan",
        "koboldcpp",
        "exllamav2",
        "mlx",
    ],
    "cloud_model_prefixes": [
        "gemini",
        "gpt-4",
        "gpt-5",
        "gpt-3.5",
        "o1-",
        "o3-",
        "o4-",
        "claude-",
        "copilot",
        "grok-",
        "sonar-",
        "command-r",
        "deepseek/deepseek-",
    ],
    "local_model_tokens": [
        ".gguf",
        "-gguf",
        ":32b",
        ":14b",
        ":8b",
        ":7b",
        "awq",
        "gptq",
        "exl2",
        "mlx",
        "ollama",
        "lemonade",
        "vllm",
    ],
}


class SqliteTelemetryReader:
    def __init__(
        self,
        tpm_ceiling: int = 2_000_000,
        rpm_ceiling: int = 1000,
        rpd_ceiling: int = 10000,
        hermes_cfg: Optional[Dict[str, Any]] = None,
        quotas_cfg: Optional[Dict[str, Any]] = None,
        routing_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.tpm_ceiling = int(tpm_ceiling)
        self.rpm_ceiling = int(rpm_ceiling)
        self.rpd_ceiling = int(rpd_ceiling)
        self.hermes_cfg: Dict[str, Any] = dict(hermes_cfg or {})
        self.quotas_cfg: Dict[str, Any] = dict(quotas_cfg or {})
        self.routing_cfg: Dict[str, List[str]] = {
            k: list(v) for k, v in DEFAULT_ROUTING_CFG.items()
        }
        if routing_cfg:
            self.configure(routing_cfg=routing_cfg)

    def configure(
        self,
        hermes_cfg: Optional[Dict[str, Any]] = None,
        quotas_cfg: Optional[Dict[str, Any]] = None,
        routing_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        if hermes_cfg is not None:
            self.hermes_cfg = dict(hermes_cfg)
        if quotas_cfg is not None:
            self.quotas_cfg = dict(quotas_cfg)
            d_cfg = self.quotas_cfg.get("default") or {}
            if isinstance(d_cfg, dict):
                self.tpm_ceiling = int(d_cfg.get("tpm_ceiling", self.tpm_ceiling))
                self.rpm_ceiling = int(d_cfg.get("rpm_ceiling", self.rpm_ceiling))
                self.rpd_ceiling = int(d_cfg.get("rpd_ceiling", self.rpd_ceiling))
        if routing_cfg is not None:
            for k, default_list in DEFAULT_ROUTING_CFG.items():
                vals = routing_cfg.get(k)
                if isinstance(vals, list) and vals:
                    self.routing_cfg[k] = [str(x).strip().lower() for x in vals if str(x).strip()]
                else:
                    self.routing_cfg[k] = list(default_list)

    @staticmethod
    def _connect_ro(path: Path) -> Optional[sqlite3.Connection]:
        if not path.is_file():
            return None
        try:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        except sqlite3.Error:
            return None

    @staticmethod
    def _norm_ts(raw_ts: Any) -> float:
        if raw_ts is None:
            return 0.0
        try:
            val = float(raw_ts)
        except (TypeError, ValueError):
            return 0.0
        return val / 1000.0 if val > 1e12 else val

    @staticmethod
    def _parse_opencode_model(raw_model: Any) -> Tuple[str, str]:
        if not raw_model:
            return "Unknown", ""
        if isinstance(raw_model, str) and raw_model.startswith("{"):
            try:
                obj = json.loads(raw_model)
                return str(obj.get("id") or raw_model), str(obj.get("providerID") or "")
            except json.JSONDecodeError:
                return raw_model, ""
        return str(raw_model), ""

    @staticmethod
    def _extract_model_config_hints(raw_cfg: Any) -> Tuple[str, str]:
        if not raw_cfg:
            return "", ""
        obj: Any = None
        if isinstance(raw_cfg, dict):
            obj = raw_cfg
        elif isinstance(raw_cfg, str) and raw_cfg.strip().startswith("{"):
            try:
                obj = json.loads(raw_cfg)
            except json.JSONDecodeError:
                obj = None
        if not isinstance(obj, dict):
            return "", ""
        raw_gw = obj.get("gateway_runtime")
        gw: Dict[str, Any] = raw_gw if isinstance(raw_gw, dict) else {}
        prov = str(gw.get("provider") or obj.get("provider") or "").strip()
        url = str(gw.get("base_url") or obj.get("base_url") or "").strip()
        return prov, url

    def classify_session(
        self,
        model: str,
        provider: str = "",
        base_url: str = "",
        model_config_raw: Any = None,
    ) -> Tuple[str, str]:
        """
        Classification stricte à 3 niveaux (Endpoint base_url > Provider > Famille modèle).
        Retourne ('local' | 'cloud', target_info_label).
        Évite tout faux positif quand un outil local (hermes/opencode/orca/copilot) lance un LLM Cloud.
        """
        mc_prov, mc_url = self._extract_model_config_hints(model_config_raw)
        eff_prov = str(provider or mc_prov or "").strip()
        eff_prov_low = eff_prov.lower()
        eff_url = str(base_url or mc_url or "").strip()
        m_str = str(model or "").strip()
        m_low = m_str.lower()

        host = ""
        port_txt = ""
        if eff_url:
            try:
                parsed = urllib.parse.urlparse(eff_url)
                host = (parsed.hostname or "").lower()
                if parsed.port:
                    port_txt = f":{parsed.port}"
            except ValueError:
                host = ""

        cloud_hosts = self.routing_cfg["cloud_hosts"]
        local_hosts = self.routing_cfg["local_hosts"]
        cloud_providers = self.routing_cfg["cloud_providers"]
        local_providers = self.routing_cfg["local_providers"]
        cloud_prefixes = self.routing_cfg["cloud_model_prefixes"]
        local_tokens = self.routing_cfg["local_model_tokens"]

        # Niveau 1 : Endpoint Cloud explicite (priorité absolue)
        if host and any(host == ch or host.endswith("." + ch) for ch in cloud_hosts):
            lbl = eff_prov if (eff_prov_low and eff_prov_low != "custom") else host
            return "cloud", lbl

        # Niveau 2 : Provider Cloud explicite (même derrière proxy local)
        prov_tokens = [p for p in re.split(r"[:/\s()]+", eff_prov_low) if p]
        is_explicit_cloud_prov = any(
            cp == eff_prov_low or cp in prov_tokens or eff_prov_low.startswith(cp + ":")
            for cp in cloud_providers
        )
        if is_explicit_cloud_prov:
            return "cloud", eff_prov

        # Niveau 3a : Famille de modèle strictement Cloud (sauf artefact GGUF sur endpoint local)
        is_gguf = (".gguf" in m_low) or ("-gguf" in m_low)
        if not is_gguf and any(m_low.startswith(pref) or pref in m_low for pref in cloud_prefixes):
            return "cloud", (eff_prov if (eff_prov_low and eff_prov_low != "custom") else "cloud")

        # Niveau 1b / 2b : Endpoint local (localhost/127.0.0.1) ou Provider local explicite
        is_explicit_local_prov = (
            eff_prov_low.startswith("custom:local")
            or any(lp == eff_prov_low or lp in prov_tokens for lp in local_providers)
        )
        is_local_host = bool(host and host in local_hosts)

        if is_explicit_local_prov or is_local_host:
            if "lemonade" in eff_prov_low:
                lbl = f"lemonade{port_txt}" if port_txt else "lemonade"
            elif "ollama" in eff_prov_low or port_txt == ":11434":
                lbl = f"ollama{port_txt}" if port_txt else "ollama"
            elif eff_prov_low and eff_prov_low != "custom":
                lbl = eff_prov
            elif port_txt:
                lbl = f"localhost{port_txt}"
            else:
                lbl = "local"
            return "local", lbl

        # Niveau 3b : Fallback modèle local explicite uniquement si artefacts locaux forts
        if any(tok in m_low for tok in local_tokens):
            return "local", (eff_prov if (eff_prov_low and eff_prov_low != "custom") else "local")

        return "cloud", (eff_prov or "cloud")

    def _resolve_model_quota(self, model_name: str) -> Dict[str, Any]:
        default_cfg = self.quotas_cfg.get("default") or {}
        d_tpm = int(default_cfg.get("tpm_ceiling", self.tpm_ceiling))
        d_rpm = int(default_cfg.get("rpm_ceiling", self.rpm_ceiling))
        d_rpd = int(default_cfg.get("rpd_ceiling", self.rpd_ceiling))

        models_map = self.quotas_cfg.get("models") or {}
        m_low = str(model_name or "").strip().lower()
        if isinstance(models_map, dict) and m_low:
            for pat, cfg in models_map.items():
                if isinstance(cfg, dict) and str(pat).strip().lower() == m_low:
                    return {
                        "label": str(cfg.get("label") or model_name),
                        "tpm_ceiling": int(cfg.get("tpm_ceiling", d_tpm)),
                        "rpm_ceiling": int(cfg.get("rpm_ceiling", d_rpm)),
                        "rpd_ceiling": int(cfg.get("rpd_ceiling", d_rpd)),
                    }
            for pat in sorted(models_map.keys(), key=lambda x: len(str(x)), reverse=True):
                cfg = models_map[pat]
                if isinstance(cfg, dict) and fnmatch.fnmatchcase(m_low, str(pat).strip().lower()):
                    return {
                        "label": str(cfg.get("label") or model_name),
                        "tpm_ceiling": int(cfg.get("tpm_ceiling", d_tpm)),
                        "rpm_ceiling": int(cfg.get("rpm_ceiling", d_rpm)),
                        "rpd_ceiling": int(cfg.get("rpd_ceiling", d_rpd)),
                    }

        return {
            "label": str(model_name or default_cfg.get("label") or "Cloud"),
            "tpm_ceiling": d_tpm,
            "rpm_ceiling": d_rpm,
            "rpd_ceiling": d_rpd,
        }

    @staticmethod
    def _clean_prompt_text(raw_text: Any) -> str:
        if not raw_text:
            return ""
        txt = str(raw_text)
        if txt.lstrip().startswith("[CONTEXT COMPACTION"):
            return ""
        txt = txt.split("--- Attached Context ---")[0]
        txt = re.sub(r"@file:\S+\s*", "", txt)
        txt = re.sub(r"\s+", " ", txt).strip()
        if len(txt) > 115:
            txt = txt[:112].rstrip() + "..."
        return txt

    def _read_opencode_db(self, now: float, active_window_sec: float, max_sessions: int) -> Dict[str, Any]:
        db_path = Path.home() / ".local/share/opencode/opencode.db"
        conn = self._connect_ro(db_path)
        result: Dict[str, Any] = {
            "available": False,
            "latest_ts": 0.0,
            "latest_session": None,
            "sessions": [],
            "cloud_sessions": [],
            "windows": {"SESSION": 0, "1H": 0, "4H": 0, "24H": 0, "7J": 0, "TOTAL": 0},
            "local_cumul_windows": {"1H": 0, "4H": 0, "24H": 0},
            "local_window_sessions": {"1H": 0, "4H": 0, "24H": 0},
            "cloud_windows": {"1H": 0, "4H": 0, "24H": 0},
            "cloud_window_sessions": {"1H": 0, "4H": 0, "24H": 0},
        }
        if conn is None:
            return result

        try:
            cur = conn.cursor()
            cols = {r[1] for r in cur.execute("PRAGMA table_info(session)").fetchall()}
            if not cols:
                return result
            result["available"] = True
            parent_sel = "parent_id" if "parent_id" in cols else "NULL AS parent_id"
            agent_sel = "agent" if "agent" in cols else "'build' AS agent"
            rows = cur.execute(
                f"""
                SELECT id, title, directory, time_created, time_updated, model,
                       tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write,
                       {agent_sel}, {parent_sel}
                FROM session
                ORDER BY COALESCE(time_updated, time_created) DESC
                LIMIT 200
                """
            ).fetchall()

            for idx, r in enumerate(rows):
                sid, title, directory, t_cre, t_upd, model_raw, t_in, t_out, t_rsn, t_cr, t_cw, agent_raw, parent_id = r
                ts = self._norm_ts(t_upd or t_cre)
                started_ts_oc = self._norm_ts(t_cre) or ts
                tok_sum = int(t_in or 0) + int(t_out or 0) + int(t_rsn or 0) + int(t_cr or 0)
                age = max(0.0, now - ts)
                model_clean, provider_id = self._parse_opencode_model(model_raw)
                agent_clean = str(agent_raw or "build").upper()
                loc_kind, target_info = self.classify_session(model_clean, provider=provider_id)

                tool_cnt = 0
                last_prompt_oc = ""
                if idx < max_sessions:
                    try:
                        tool_cnt = int(
                            cur.execute(
                                "SELECT COUNT(*) FROM part WHERE session_id = ? AND json_extract(data, '$.type') = 'tool'",
                                (sid,),
                            ).fetchone()[0]
                            or 0
                        )
                    except sqlite3.Error:
                        tool_cnt = 0
                    try:
                        p_rows = cur.execute(
                            """
                            SELECT json_extract(p.data, '$.text')
                            FROM message m
                            JOIN part p ON p.message_id = m.id
                            WHERE m.session_id = ?
                              AND json_extract(m.data, '$.role') = 'user'
                              AND json_extract(p.data, '$.type') = 'text'
                            ORDER BY m.time_created DESC, p.time_created DESC
                            LIMIT 5
                            """,
                            (sid,),
                        ).fetchall()
                        for (raw_txt,) in p_rows:
                            if not raw_txt or str(raw_txt).lstrip().startswith("Continue if you have next steps"):
                                continue
                            cleaned = self._clean_prompt_text(raw_txt)
                            if cleaned:
                                last_prompt_oc = cleaned
                                break
                    except sqlite3.Error:
                        last_prompt_oc = ""

                if loc_kind == "local":
                    sess_obj = {
                        "id": sid,
                        "parent_id": parent_id or None,
                        "is_subagent": bool(parent_id),
                        "source": "opencode",
                        "target": "LOCAL",
                        "target_info": target_info,
                        "title": title or "OpenCode Session",
                        "directory": directory or "",
                        "model": model_clean,
                        "agent": agent_clean,
                        "tool_calls": tool_cnt,
                        "tokens_in": int(t_in or 0),
                        "tokens_out": int(t_out or 0),
                        "tokens_rsn": int(t_rsn or 0),
                        "tokens_cache_read": int(t_cr or 0),
                        "tokens_total": tok_sum,
                        "last_prompt": last_prompt_oc,
                        "started_at": round(started_ts_oc, 1),
                        "updated_at": ts,
                        "age_sec": round(age),
                        "status": "ACTIVE" if age <= active_window_sec else "ENDED",
                    }
                    if result["latest_session"] is None:
                        result["latest_ts"] = ts
                        result["latest_session"] = sess_obj
                        result["windows"]["SESSION"] = tok_sum
                    if len(result["sessions"]) < max_sessions and (tok_sum > 0 or age <= 86400):
                        result["sessions"].append(sess_obj)

                    if age <= 3600:
                        result["windows"]["1H"] += tok_sum
                    if age <= 14400:
                        result["windows"]["4H"] += tok_sum
                    if age <= 86400:
                        result["windows"]["24H"] += tok_sum
                    if age <= 7 * 86400:
                        result["windows"]["7J"] += tok_sum
                    result["windows"]["TOTAL"] += tok_sum

                    if age <= 86400 and tok_sum > 0:
                        added_lw = {"1H": False, "4H": False, "24H": False}
                        msg_loc_24h = 0
                        try:
                            m_rows_loc = cur.execute(
                                """
                                SELECT time_created,
                                       COALESCE(json_extract(data, '$.tokens.input'), 0) +
                                       COALESCE(json_extract(data, '$.tokens.output'), 0) +
                                       COALESCE(json_extract(data, '$.tokens.reasoning'), 0) +
                                       COALESCE(json_extract(data, '$.tokens.cache.read'), 0) +
                                       COALESCE(json_extract(data, '$.tokens.cache.write'), 0)
                                FROM message
                                WHERE session_id = ?
                                """,
                                (sid,),
                            ).fetchall()
                            for mts_raw, m_tok in m_rows_loc:
                                mts = self._norm_ts(mts_raw)
                                m_age = max(0.0, now - mts)
                                mt = int(m_tok or 0)
                                if mt <= 0 or m_age > 86400:
                                    continue
                                msg_loc_24h += mt
                                if m_age <= 3600:
                                    result["local_cumul_windows"]["1H"] += mt
                                    added_lw["1H"] = True
                                if m_age <= 14400:
                                    result["local_cumul_windows"]["4H"] += mt
                                    added_lw["4H"] = True
                                result["local_cumul_windows"]["24H"] += mt
                                added_lw["24H"] = True
                        except sqlite3.Error:
                            msg_loc_24h = 0
                        if msg_loc_24h == 0:
                            if age <= 3600:
                                result["local_cumul_windows"]["1H"] += tok_sum
                                added_lw["1H"] = True
                            if age <= 14400:
                                result["local_cumul_windows"]["4H"] += tok_sum
                                added_lw["4H"] = True
                            result["local_cumul_windows"]["24H"] += tok_sum
                            added_lw["24H"] = True
                        for wk, flag in added_lw.items():
                            if flag:
                                result["local_window_sessions"][wk] += 1
                else:
                    if age <= 86400 and tok_sum > 0:
                        added_w = {"1H": False, "4H": False, "24H": False}
                        msg_sum_24h = 0
                        try:
                            m_rows_oc = cur.execute(
                                """
                                SELECT time_created,
                                       COALESCE(json_extract(data, '$.tokens.input'), 0) +
                                       COALESCE(json_extract(data, '$.tokens.output'), 0) +
                                       COALESCE(json_extract(data, '$.tokens.reasoning'), 0) +
                                       COALESCE(json_extract(data, '$.tokens.cache.read'), 0) +
                                       COALESCE(json_extract(data, '$.tokens.cache.write'), 0)
                                FROM message
                                WHERE session_id = ?
                                """,
                                (sid,),
                            ).fetchall()
                            for mts_raw, m_tok in m_rows_oc:
                                mts = self._norm_ts(mts_raw)
                                m_age = max(0.0, now - mts)
                                mt = int(m_tok or 0)
                                if mt <= 0 or m_age > 86400:
                                    continue
                                msg_sum_24h += mt
                                if m_age <= 3600:
                                    result["cloud_windows"]["1H"] += mt
                                    added_w["1H"] = True
                                if m_age <= 14400:
                                    result["cloud_windows"]["4H"] += mt
                                    added_w["4H"] = True
                                result["cloud_windows"]["24H"] += mt
                                added_w["24H"] = True
                        except sqlite3.Error:
                            msg_sum_24h = 0
                        if msg_sum_24h == 0:
                            if age <= 3600:
                                result["cloud_windows"]["1H"] += tok_sum
                                added_w["1H"] = True
                            if age <= 14400:
                                result["cloud_windows"]["4H"] += tok_sum
                                added_w["4H"] = True
                            result["cloud_windows"]["24H"] += tok_sum
                            added_w["24H"] = True
                        for wk, flag in added_w.items():
                            if flag:
                                result["cloud_window_sessions"][wk] += 1

                    q_meta = self._resolve_model_quota(model_clean)
                    if len(result["cloud_sessions"]) < max_sessions and (tok_sum > 0 or age <= 86400):
                        result["cloud_sessions"].append({
                            "id": str(sid),
                            "parent_session_id": str(parent_id) if parent_id else None,
                            "is_subagent": bool(parent_id),
                            "model": model_clean,
                            "model_label": q_meta["label"],
                            "title": title or "OpenCode Cloud Session",
                            "tool_calls": tool_cnt,
                            "api_calls": 0,
                            "profile": "opencode",
                            "source": "opencode",
                            "target": "CLOUD",
                            "target_info": target_info,
                            "cwd": directory or "",
                            "provider": provider_id or target_info,
                            "status": "ACTIVE" if age <= active_window_sec else "ENDED",
                            "tokens_in": int(t_in or 0),
                            "tokens_out": int(t_out or 0),
                            "tokens_rsn": int(t_rsn or 0),
                            "tokens_cache_read": int(t_cr or 0),
                            "tokens_total": tok_sum,
                            "last_prompt": last_prompt_oc,
                            "started_at": round(started_ts_oc, 1),
                            "updated_at": ts,
                            "age_sec": round(age),
                        })
        except sqlite3.Error:
            pass
        finally:
            conn.close()
        return result

    def _iter_all_hermes_dbs(self) -> List[Tuple[str, Path]]:
        home = Path.home()
        candidates: List[Tuple[str, Path]] = []
        root_db = home / ".hermes/state.db"
        if root_db.is_file():
            candidates.append(("default", root_db))
        profiles_dir = home / ".hermes/profiles"
        if profiles_dir.is_dir():
            for prof in sorted(profiles_dir.iterdir()):
                db = prof / "state.db"
                if prof.is_dir() and db.is_file():
                    candidates.append((prof.name, db))
        return candidates

    def _resolve_hermes_targets(self) -> Tuple[str, bool, List[str], List[Tuple[str, Path]]]:
        """Résoudre le profil le plus récent tout en agrégeant tous les profils par défaut."""
        all_dbs = self._iter_all_hermes_dbs()
        avail_names = [name for name, _ in all_dbs]
        if not all_dbs:
            return "default", True, [], []

        include_all = bool(self.hermes_cfg.get("include_all_profiles", True))
        env_all = os.environ.get("TOKENWATCHER_HERMES_ALL_PROFILES", "").strip().lower()
        if env_all in {"1", "true", "yes"}:
            include_all = True
        elif env_all in {"0", "false", "no"}:
            include_all = False

        requested = (
            os.environ.get("TOKENWATCHER_HERMES_PROFILE")
            or os.environ.get("HERMES_PROFILE")
            or str(self.hermes_cfg.get("active_profile") or "auto")
        ).strip()

        if requested and requested.lower() != "auto":
            matched = [(n, p) for n, p in all_dbs if n == requested]
            if matched:
                return requested, include_all, avail_names, (all_dbs if include_all else matched)

        best_name = all_dbs[0][0]
        best_score = -1.0
        for name, db_path in all_dbs:
            score = 0.0
            conn = self._connect_ro(db_path)
            if conn is not None:
                try:
                    row = conn.execute(
                        "SELECT MAX(COALESCE(last_activity_at, started_at)) FROM sessions"
                    ).fetchone()
                    if row and row[0]:
                        score = self._norm_ts(row[0])
                except sqlite3.Error:
                    pass
                finally:
                    conn.close()
            if score <= 0.0:
                try:
                    score = db_path.stat().st_mtime * 0.001
                except OSError:
                    score = 0.0
            if score > best_score:
                best_score = score
                best_name = name

        if include_all:
            return best_name, True, avail_names, all_dbs
        return best_name, False, avail_names, [(n, p) for n, p in all_dbs if n == best_name]

    def _read_hermes_dbs(self, now: float, active_window_sec: float, max_sessions: int) -> Dict[str, Any]:
        resolved_profile, include_all, avail_profiles, target_dbs = self._resolve_hermes_targets()
        all_dbs = self._iter_all_hermes_dbs()
        cloud_allowed_profiles = {n for n, _ in target_dbs}
        result: Dict[str, Any] = {
            "available": False,
            "active_profile": resolved_profile,
            "include_all_profiles": include_all,
            "available_profiles": avail_profiles,
            "latest_ts": 0.0,
            "latest_session": None,
            "latest_cloud_ts": 0.0,
            "latest_cloud_session": None,
            "latest_local_ts": 0.0,
            "latest_local_session": None,
            "cloud_sessions": [],
            "local_sessions": [],
            "windows": {"SESSION": 0, "1H": 0, "4H": 0, "24H": 0, "7J": 0, "TOTAL": 0},
            "local_windows": {"SESSION": 0, "1H": 0, "4H": 0, "24H": 0, "7J": 0, "TOTAL": 0},
            "local_cumul_windows": {"1H": 0, "4H": 0, "24H": 0},
            "local_window_sessions": {"1H": 0, "4H": 0, "24H": 0},
            "cloud_windows": {"1H": 0, "4H": 0, "24H": 0},
            "cloud_window_sessions": {"1H": 0, "4H": 0, "24H": 0},
            "cloud_tpm_60s": 0,
            "cloud_rpm_60s": 0,
            "cloud_rpd_24h": 0,
            "cloud_in_total": 0,
            "cloud_out_total": 0,
            "cloud_rsn_total": 0,
            "cloud_cached_total": 0,
            "by_model": [],
            "by_profile": [],
        }

        cloud_sessions_all: List[Dict[str, Any]] = []
        local_sessions_all: List[Dict[str, Any]] = []
        recent_cloud_for_win: List[Tuple[Path, str, float, float, int]] = []
        recent_local_for_win: List[Tuple[Path, str, float, float, int]] = []
        model_stats: Dict[str, Dict[str, Any]] = {}
        profile_stats: Dict[str, Dict[str, Any]] = {}
        db_by_sid: Dict[Tuple[str, str], Path] = {}

        def _ensure_model(m_name: str) -> Dict[str, Any]:
            if m_name not in model_stats:
                q_cfg = self._resolve_model_quota(m_name)
                model_stats[m_name] = {
                    "model": m_name,
                    "label": q_cfg["label"],
                    "tpm_60s": 0,
                    "tpm_ceiling": q_cfg["tpm_ceiling"],
                    "rpm_60s": 0,
                    "rpm_ceiling": q_cfg["rpm_ceiling"],
                    "rpd_24h": 0,
                    "rpd_ceiling": q_cfg["rpd_ceiling"],
                    "in_24h": 0,
                    "out_24h": 0,
                    "rsn_24h": 0,
                    "cached_24h": 0,
                    "active_sessions": 0,
                    "open_sessions": 0,
                    "latest_ts": 0.0,
                }
            return model_stats[m_name]

        for prof_default, db_path in all_dbs:
            conn = self._connect_ro(db_path)
            if conn is None:
                continue
            try:
                cur = conn.cursor()
                cols = {r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()}
                if not cols:
                    continue
                result["available"] = True
                prof_sel = "profile_name" if "profile_name" in cols else "NULL AS profile_name"
                parent_sel = "parent_session_id" if "parent_session_id" in cols else "NULL AS parent_session_id"
                ended_sel = "ended_at" if "ended_at" in cols else "NULL AS ended_at"
                source_sel = "source" if "source" in cols else "'cli' AS source"
                msg_cnt_sel = "message_count" if "message_count" in cols else "0 AS message_count"
                url_sel = "billing_base_url" if "billing_base_url" in cols else "NULL AS billing_base_url"
                mc_sel = "model_config" if "model_config" in cols else "NULL AS model_config"

                rows = cur.execute(
                    f"""
                    SELECT id, model, title, tool_call_count, input_tokens, output_tokens,
                           cache_read_tokens, reasoning_tokens, last_activity_at, started_at,
                           cwd, billing_provider, api_call_count, {prof_sel},
                           {parent_sel}, {ended_sel}, {source_sel}, {msg_cnt_sel},
                           {url_sel}, {mc_sel}
                    FROM sessions
                    ORDER BY COALESCE(last_activity_at, started_at) DESC
                    LIMIT 250
                    """
                ).fetchall()

                for r in rows:
                    (
                        sid,
                        model,
                        title,
                        tools_cnt,
                        t_in,
                        t_out,
                        t_cr,
                        t_rsn,
                        last_act,
                        started,
                        cwd,
                        provider,
                        api_calls,
                        prof_name,
                        parent_sid,
                        ended_at,
                        source_val,
                        msg_cnt,
                        base_url,
                        model_cfg_raw,
                    ) = r
                    ts = self._norm_ts(last_act or started)
                    in_v = int(t_in or 0)
                    out_v = int(t_out or 0)
                    cr_v = int(t_cr or 0)
                    rsn_v = int(t_rsn or 0)
                    tok_sum = in_v + out_v + rsn_v + cr_v
                    tools_v = int(tools_cnt or 0)
                    api_v = int(api_calls or 0)
                    msgs_v = int(msg_cnt or 0)
                    prof_label = prof_default if prof_default != "default" else str(prof_name or "default")
                    model_str = str(model or "Unknown")
                    loc_kind, target_info = self.classify_session(
                        model_str,
                        provider=str(provider or ""),
                        base_url=str(base_url or ""),
                        model_config_raw=model_cfg_raw,
                    )
                    is_local = loc_kind == "local"
                    if not is_local and not include_all and prof_default not in cloud_allowed_profiles:
                        continue
                    age = max(0.0, now - ts)

                    if ended_at is not None or age > 86400:
                        sess_status = "ENDED"
                    elif age <= active_window_sec:
                        sess_status = "ACTIVE"
                    else:
                        sess_status = "OPEN"

                    if ended_at is not None and msgs_v == 0 and api_v == 0 and tools_v == 0:
                        continue

                    if ts > result["latest_ts"]:
                        result["latest_ts"] = ts
                        result["latest_session"] = {
                            "id": sid,
                            "model": model_str,
                            "title": title or "Hermes Session",
                            "tool_calls": tools_v,
                            "api_calls": api_v,
                            "profile": prof_label,
                            "tokens_in": in_v,
                            "tokens_out": out_v,
                            "tokens_rsn": rsn_v,
                            "tokens_cache_read": cr_v,
                            "tokens_total": tok_sum,
                            "cwd": cwd or "",
                            "provider": provider or ("local" if is_local else "cloud"),
                            "updated_at": ts,
                        }
                        result["windows"]["SESSION"] = tok_sum

                    if age <= 3600:
                        result["windows"]["1H"] += tok_sum
                    if age <= 14400:
                        result["windows"]["4H"] += tok_sum
                    if age <= 86400:
                        result["windows"]["24H"] += tok_sum
                    if age <= 7 * 86400:
                        result["windows"]["7J"] += tok_sum
                    result["windows"]["TOTAL"] += tok_sum

                    if not is_local:
                        db_by_sid[(prof_label, str(sid))] = db_path
                        started_ts = self._norm_ts(started) or ts
                        if age <= 86400 and tok_sum > 0:
                            recent_cloud_for_win.append((db_path, str(sid), ts, started_ts, tok_sum))
                        p_stat = profile_stats.setdefault(
                            prof_label,
                            {
                                "profile": prof_label,
                                "active_sessions": 0,
                                "open_sessions": 0,
                                "total_sessions": 0,
                                "latest_ts": 0.0,
                            },
                        )
                        p_stat["total_sessions"] += 1
                        if ts > p_stat["latest_ts"]:
                            p_stat["latest_ts"] = ts
                        if sess_status == "ACTIVE":
                            p_stat["active_sessions"] += 1
                        elif sess_status == "OPEN":
                            p_stat["open_sessions"] += 1
                        q_meta = self._resolve_model_quota(model_str)
                        s_entry = {
                            "id": str(sid),
                            "parent_session_id": str(parent_sid) if parent_sid else None,
                            "is_subagent": bool(parent_sid),
                            "model": model_str,
                            "model_label": q_meta["label"],
                            "title": title or "Hermes Cloud Session",
                            "tool_calls": tools_v,
                            "api_calls": api_v,
                            "profile": prof_label,
                            "source": "hermes",
                            "cli_source": str(source_val or "cli"),
                            "target": "CLOUD",
                            "target_info": target_info,
                            "cwd": cwd or "",
                            "provider": str(provider or target_info or "cloud"),
                            "status": sess_status,
                            "tokens_in": in_v,
                            "tokens_out": out_v,
                            "tokens_rsn": rsn_v,
                            "tokens_cache_read": cr_v,
                            "tokens_total": tok_sum,
                            "last_prompt": "",
                            "started_at": round(started_ts, 1),
                            "updated_at": ts,
                            "age_sec": round(age),
                        }
                        cloud_sessions_all.append(s_entry)

                        if ts > result["latest_cloud_ts"]:
                            result["latest_cloud_ts"] = ts
                            result["latest_cloud_session"] = s_entry

                        if sess_status in {"ACTIVE", "OPEN"} or age <= 86400:
                            ms = _ensure_model(model_str)
                            if ts > ms["latest_ts"]:
                                ms["latest_ts"] = ts
                            if sess_status == "ACTIVE":
                                ms["active_sessions"] += 1
                            elif sess_status == "OPEN":
                                ms["open_sessions"] += 1
                            if age <= 86400:
                                calls_inc = api_v if api_v > 0 else (1 if msgs_v > 0 else 0)
                                ms["rpd_24h"] += calls_inc
                                ms["in_24h"] += in_v
                                ms["out_24h"] += out_v
                                ms["rsn_24h"] += rsn_v
                                ms["cached_24h"] += cr_v
                                result["cloud_rpd_24h"] += calls_inc
                                result["cloud_in_total"] += in_v
                                result["cloud_out_total"] += out_v
                                result["cloud_rsn_total"] += rsn_v
                                result["cloud_cached_total"] += cr_v
                    else:
                        db_by_sid[(prof_label, str(sid))] = db_path
                        started_ts_loc = self._norm_ts(started) or ts
                        if age <= 86400 and tok_sum > 0:
                            recent_local_for_win.append((db_path, str(sid), ts, started_ts_loc, tok_sum))
                        loc_entry = {
                            "id": str(sid),
                            "parent_id": str(parent_sid) if parent_sid else None,
                            "is_subagent": bool(parent_sid),
                            "profile": prof_label,
                            "source": "hermes",
                            "target": "LOCAL",
                            "target_info": target_info,
                            "model": model_str,
                            "title": title or "Hermes Local Session",
                            "directory": cwd or "",
                            "agent": f"HERMES ({prof_label})",
                            "tool_calls": tools_v,
                            "tokens_in": in_v,
                            "tokens_out": out_v,
                            "tokens_rsn": rsn_v,
                            "tokens_cache_read": cr_v,
                            "tokens_total": tok_sum,
                            "provider": provider or target_info or "local",
                            "last_prompt": "",
                            "started_at": round(started_ts_loc, 1),
                            "updated_at": ts,
                            "age_sec": round(age),
                            "status": sess_status,
                        }
                        local_sessions_all.append(loc_entry)
                        if ts > result["latest_local_ts"]:
                            result["latest_local_ts"] = ts
                            result["latest_local_session"] = loc_entry
                            result["local_windows"]["SESSION"] = tok_sum
                        if age <= 3600:
                            result["local_windows"]["1H"] += tok_sum
                        if age <= 14400:
                            result["local_windows"]["4H"] += tok_sum
                        if age <= 86400:
                            result["local_windows"]["24H"] += tok_sum
                        if age <= 7 * 86400:
                            result["local_windows"]["7J"] += tok_sum
                        result["local_windows"]["TOTAL"] += tok_sum

                msg_cols = {r[1] for r in cur.execute("PRAGMA table_info(messages)").fetchall()}
                if "timestamp" in msg_cols and "token_count" in msg_cols:
                    cutoff_60s = now - 60.0
                    m_url_sel = "s.billing_base_url" if "billing_base_url" in cols else "NULL AS billing_base_url"
                    m_mc_sel = "s.model_config" if "model_config" in cols else "NULL AS model_config"
                    m_rows = cur.execute(
                        f"""
                        SELECT m.timestamp, m.token_count, s.model, s.billing_provider,
                               s.input_tokens, s.output_tokens, s.reasoning_tokens, s.api_call_count,
                               {m_url_sel}, {m_mc_sel}
                        FROM messages m
                        JOIN sessions s ON m.session_id = s.id
                        WHERE m.timestamp >= ?
                        """,
                        (cutoff_60s,),
                    ).fetchall()
                    for mts, mtok, smodel, sprov, s_in, s_out, s_rsn, s_calls, s_url, s_mc in m_rows:
                        s_mod_str = str(smodel or "Unknown")
                        s_kind, _ = self.classify_session(
                            s_mod_str,
                            provider=str(sprov or ""),
                            base_url=str(s_url or ""),
                            model_config_raw=s_mc,
                        )
                        if s_kind == "cloud":
                            if mtok is not None and int(mtok) > 0:
                                tok_inc = int(mtok)
                            else:
                                tok_inc = (int(s_in or 0) + int(s_out or 0) + int(s_rsn or 0)) // max(1, int(s_calls or 1))
                            result["cloud_rpm_60s"] += 1
                            result["cloud_tpm_60s"] += tok_inc
                            ms = _ensure_model(s_mod_str)
                            ms["rpm_60s"] += 1
                            ms["tpm_60s"] += tok_inc
            except sqlite3.Error:
                pass
            finally:
                conn.close()

        status_rank = {"ACTIVE": 0, "OPEN": 1, "ENDED": 1}
        cloud_sessions_all.sort(key=lambda s: (status_rank.get(s["status"], 9), -float(s["updated_at"])))
        top_cloud_list: List[Dict[str, Any]] = []
        seen_cloud_keys: set = set()
        # Garantir au moins les 3 sessions les plus récentes par profil, puis compléter jusqu'à max_sessions
        per_prof_counts: Dict[str, int] = {}
        for s_item in cloud_sessions_all:
            pk = str(s_item.get("profile") or "default")
            if per_prof_counts.get(pk, 0) < 3:
                per_prof_counts[pk] = per_prof_counts.get(pk, 0) + 1
                s_key = (pk, str(s_item.get("id") or ""))
                seen_cloud_keys.add(s_key)
                top_cloud_list.append(s_item)
        for s_item in cloud_sessions_all:
            if len(top_cloud_list) >= max(max_sessions, len(top_cloud_list)):
                break
            pk = str(s_item.get("profile") or "default")
            s_key = (pk, str(s_item.get("id") or ""))
            if s_key not in seen_cloud_keys:
                seen_cloud_keys.add(s_key)
                top_cloud_list.append(s_item)
        top_cloud_list.sort(key=lambda s: (status_rank.get(s["status"], 9), -float(s["updated_at"])))
        top_cloud = top_cloud_list

        local_sessions_all.sort(key=lambda s: (status_rank.get(s["status"], 9), -float(s["updated_at"])))
        top_local = local_sessions_all[:max_sessions]

        by_db: Dict[Path, List[Dict[str, Any]]] = {}
        for s_item in top_cloud + top_local:
            dbp = db_by_sid.get((str(s_item.get("profile") or "default"), str(s_item["id"])))
            if dbp is not None:
                by_db.setdefault(dbp, []).append(s_item)

        for dbp, items in by_db.items():
            conn = self._connect_ro(dbp)
            if conn is None:
                continue
            try:
                for s_item in items:
                    rows_p = conn.execute(
                        "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY timestamp DESC, id DESC LIMIT 5",
                        (s_item["id"],),
                    ).fetchall()
                    for (raw_c,) in rows_p:
                        cleaned = self._clean_prompt_text(raw_c)
                        if cleaned:
                            s_item["last_prompt"] = cleaned
                            break
            except sqlite3.Error:
                pass
            finally:
                conn.close()

        # Agrégation cumulée des tokens Cloud & Locaux par fenêtre glissante (1H / 4H / 24H)
        cut_1h = now - 3600.0
        cut_4h = now - 14400.0
        cut_24h = now - 86400.0

        def _accumulate_hermes_windows(
            recent_items: List[Tuple[Path, str, float, float, int]],
            win_target: Dict[str, int],
            sess_target: Dict[str, int],
        ) -> None:
            win_by_db: Dict[Path, List[Tuple[str, float, float, int]]] = {}
            for dbp, sid, ts, started_ts, tok_sum in recent_items:
                if started_ts >= cut_1h:
                    for wk in ("1H", "4H", "24H"):
                        win_target[wk] += tok_sum
                        sess_target[wk] += 1
                else:
                    win_by_db.setdefault(dbp, []).append((sid, ts, started_ts, tok_sum))

            for dbp, w_items in win_by_db.items():
                conn = self._connect_ro(dbp)
                if conn is None:
                    continue
                try:
                    cur_w = conn.cursor()
                    for sid, ts, started_ts, tok_sum in w_items:
                        row_w = cur_w.execute(
                            """
                            SELECT
                              COUNT(*),
                              SUM(CASE WHEN role = 'assistant' THEN 1 ELSE 0 END),
                              SUM(CASE WHEN timestamp >= ? THEN COALESCE(token_count, 0) ELSE 0 END),
                              SUM(CASE WHEN timestamp >= ? THEN COALESCE(token_count, 0) ELSE 0 END),
                              SUM(CASE WHEN timestamp >= ? THEN COALESCE(token_count, 0) ELSE 0 END),
                              SUM(CASE WHEN timestamp >= ? AND role = 'assistant' THEN 1 ELSE 0 END),
                              SUM(CASE WHEN timestamp >= ? AND role = 'assistant' THEN 1 ELSE 0 END),
                              SUM(CASE WHEN timestamp >= ? AND role = 'assistant' THEN 1 ELSE 0 END),
                              SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END),
                              SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END),
                              SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END)
                            FROM messages
                            WHERE session_id = ?
                            """,
                            (cut_1h, cut_4h, cut_24h, cut_1h, cut_4h, cut_24h, cut_1h, cut_4h, cut_24h, sid),
                        ).fetchone()
                        if not row_w:
                            continue
                        tot_all, tot_asst, tc_1h, tc_4h, tc_24h, asst_1h, asst_4h, asst_24h, all_1h, all_4h, all_24h = [
                            int(v or 0) for v in row_w
                        ]
                        win_specs = (
                            ("1H", cut_1h, tc_1h, asst_1h, all_1h),
                            ("4H", cut_4h, tc_4h, asst_4h, all_4h),
                            ("24H", cut_24h, tc_24h, asst_24h, all_24h),
                        )
                        for wk, cutoff, tc_w, asst_w, all_w in win_specs:
                            if started_ts >= cutoff:
                                w_tok = tok_sum
                            elif tc_w > 0:
                                w_tok = tc_w
                            elif tot_asst > 0:
                                w_tok = round(tok_sum * (asst_w / tot_asst))
                            elif tot_all > 0:
                                w_tok = round(tok_sum * (all_w / tot_all))
                            else:
                                w_tok = tok_sum if ts >= cutoff else 0
                            if w_tok > 0:
                                win_target[wk] += w_tok
                                sess_target[wk] += 1
                except sqlite3.Error:
                    pass
                finally:
                    conn.close()

        _accumulate_hermes_windows(recent_cloud_for_win, result["cloud_windows"], result["cloud_window_sessions"])
        _accumulate_hermes_windows(recent_local_for_win, result["local_cumul_windows"], result["local_window_sessions"])

        result["cloud_sessions"] = top_cloud
        result["local_sessions"] = top_local
        by_prof_list = list(profile_stats.values())
        by_prof_list.sort(key=lambda p: (-int(p["active_sessions"] > 0), -float(p["latest_ts"])))
        result["by_profile"] = by_prof_list

        by_model_list: List[Dict[str, Any]] = []
        for m_name, ms in model_stats.items():
            tpm_pct = min(100, round((ms["tpm_60s"] / max(1, ms["tpm_ceiling"])) * 100))
            rpm_pct = min(100, round((ms["rpm_60s"] / max(1, ms["rpm_ceiling"])) * 100))
            rpd_pct = min(100, round((ms["rpd_24h"] / max(1, ms["rpd_ceiling"])) * 100))
            denom = ms["in_24h"] + ms["cached_24h"]
            ch_pct = round((ms["cached_24h"] / denom) * 100) if denom > 0 else 0
            if tpm_pct >= 85:
                st = "CRIT"
                st_lbl = f"CRITIQUE · {tpm_pct}% · Quota Cloud Saturé"
            elif tpm_pct >= 70:
                st = "WARN"
                st_lbl = f"ALERTE · {tpm_pct}% · Alerte Quota Cloud"
            else:
                st = "OK"
                st_lbl = f"OK · {tpm_pct}% · Nominal Cloud Quota"
            by_model_list.append(
                {
                    "model": m_name,
                    "label": ms["label"],
                    "tpm_60s": ms["tpm_60s"],
                    "tpm_ceiling": ms["tpm_ceiling"],
                    "tpm_pct": tpm_pct,
                    "tpm_headroom": max(0, ms["tpm_ceiling"] - ms["tpm_60s"]),
                    "rpm_60s": ms["rpm_60s"],
                    "rpm_ceiling": ms["rpm_ceiling"],
                    "rpm_pct": rpm_pct,
                    "rpd_24h": ms["rpd_24h"],
                    "rpd_ceiling": ms["rpd_ceiling"],
                    "rpd_pct": rpd_pct,
                    "in_24h": ms["in_24h"],
                    "out_24h": ms["out_24h"],
                    "rsn_24h": ms["rsn_24h"],
                    "cached_24h": ms["cached_24h"],
                    "cache_hit_pct": ch_pct,
                    "active_sessions": ms["active_sessions"],
                    "open_sessions": ms["open_sessions"],
                    "latest_ts": ms["latest_ts"],
                    "status": st,
                    "status_label": st_lbl,
                }
            )
        by_model_list.sort(key=lambda m: (-m["tpm_60s"], -m["active_sessions"], -m["latest_ts"]))
        result["by_model"] = by_model_list
        return result

    def _read_openwebui_db(self) -> Dict[str, Any]:
        db_path = Path.home() / ".local/share/open-webui/webui.db"
        conn = self._connect_ro(db_path)
        if conn is None:
            return {"available": False, "latest_ts": 0.0, "requests": []}
        requests: List[Dict[str, str]] = []
        latest_ts = 0.0
        try:
            cur = conn.cursor()
            rows = cur.execute(
                """
                SELECT c.id, c.title, c.updated_at, COALESCE(u.name, 'User')
                FROM chat c
                LEFT JOIN user u ON c.user_id = u.id
                ORDER BY c.updated_at DESC
                LIMIT 5
                """
            ).fetchall()
            for idx, (cid, title, upd, uname) in enumerate(rows, 1):
                ts = self._norm_ts(upd)
                if ts > latest_ts:
                    latest_ts = ts
                requests.append(
                    {
                        "id": f"REQ #{idx}",
                        "user": str(uname),
                        "prompt": str(title or "Chat OpenWebUI")[:48],
                        "in_tok": "--",
                        "out_tok": "--",
                        "speed": "--",
                    }
                )
        except sqlite3.Error:
            pass
        finally:
            conn.close()
        return {"available": True, "latest_ts": latest_ts, "requests": requests}

    def read_active_topology_and_tokens(self) -> Dict[str, Any]:
        """Lire les bases SQLite en ?mode=ro et calculer les quotas globaux & par modèle."""
        now = time.time()
        active_window_sec = float(self.hermes_cfg.get("active_session_window_sec", 120.0))
        max_sessions = int(self.hermes_cfg.get("max_sessions", 15))

        oc = self._read_opencode_db(now, active_window_sec, max_sessions)
        hm = self._read_hermes_dbs(now, active_window_sec, max_sessions)
        ow = self._read_openwebui_db()

        oc_ts = float(oc["latest_ts"] or 0.0)
        hm_cloud_ts = float(hm.get("latest_cloud_ts") or 0.0)
        hm_local_ts = float(hm.get("latest_local_ts") or 0.0)
        ow_ts = float(ow["latest_ts"] or 0.0)
        local_best_ts = max(oc_ts, hm_local_ts)

        if ow_ts > max(local_best_ts, hm_cloud_ts) and ow["requests"]:
            active_topology = 3
        elif hm_cloud_ts >= local_best_ts and hm_cloud_ts > 0:
            active_topology = 4
        elif oc_ts > 0 and hm_local_ts > 0 and abs(hm_local_ts - oc_ts) < 900:
            active_topology = 1
        elif oc_ts > hm_local_ts and oc_ts > 0:
            active_topology = 2
        elif hm_local_ts > 0:
            active_topology = 1
        else:
            active_topology = 1

        by_model = list(hm.get("by_model") or [])
        by_model_map = {m["model"]: m for m in by_model}
        for oc_cs in oc.get("cloud_sessions") or []:
            if float(oc_cs.get("age_sec") or 999999) <= 86400 or oc_cs.get("status") == "ACTIVE":
                m_name = oc_cs["model"]
                if m_name not in by_model_map:
                    qm = self._resolve_model_quota(m_name)
                    m_entry = {
                        "model": m_name,
                        "label": qm["label"],
                        "tpm_60s": 0,
                        "tpm_ceiling": qm["tpm_ceiling"],
                        "tpm_pct": 0,
                        "tpm_headroom": qm["tpm_ceiling"],
                        "rpm_60s": 0,
                        "rpm_ceiling": qm["rpm_ceiling"],
                        "rpm_pct": 0,
                        "rpd_24h": 0,
                        "rpd_ceiling": qm["rpd_ceiling"],
                        "rpd_pct": 0,
                        "in_24h": int(oc_cs.get("tokens_in") or 0),
                        "out_24h": int(oc_cs.get("tokens_out") or 0),
                        "rsn_24h": int(oc_cs.get("tokens_rsn") or 0),
                        "cached_24h": int(oc_cs.get("tokens_cache_read") or 0),
                        "cache_hit_pct": 0,
                        "active_sessions": 1 if oc_cs.get("status") == "ACTIVE" else 0,
                        "open_sessions": 0,
                        "latest_ts": float(oc_cs.get("updated_at") or 0.0),
                        "status": "OK",
                        "status_label": "OK · 0% · Nominal Cloud Quota",
                    }
                    by_model.append(m_entry)
                    by_model_map[m_name] = m_entry

        cloud_sess = hm.get("latest_cloud_session") or hm.get("latest_session") or {}
        primary_model = str(cloud_sess.get("model") or (by_model[0]["model"] if by_model else "gemini-4-hydrogen"))
        p_quota = self._resolve_model_quota(primary_model)

        hottest = max(by_model, key=lambda m: (m["tpm_pct"], m["tpm_60s"], m["latest_ts"])) if by_model else None
        if hottest and (hottest["tpm_pct"] > 0 or hottest["tpm_60s"] > 0):
            tpm_60s = int(hottest["tpm_60s"])
            tpm_ceil = int(hottest["tpm_ceiling"])
            tpm_pct = int(hottest["tpm_pct"])
            rpm_cur = int(hottest["rpm_60s"])
            rpm_ceil = int(hottest["rpm_ceiling"])
            rpd_cur = int(hottest["rpd_24h"])
            rpd_ceil = int(hottest["rpd_ceiling"])
            focus_model = hottest["model"]
        else:
            tpm_60s = int(hm["cloud_tpm_60s"])
            tpm_ceil = int(p_quota["tpm_ceiling"])
            tpm_pct = min(100, round((tpm_60s / max(1, tpm_ceil)) * 100))
            rpm_cur = int(hm["cloud_rpm_60s"])
            rpm_ceil = int(p_quota["rpm_ceiling"])
            rpd_cur = int(hm["cloud_rpd_24h"])
            rpd_ceil = int(p_quota["rpd_ceiling"])
            focus_model = primary_model

        tpm_free = max(0, tpm_ceil - tpm_60s)
        if tpm_pct >= 85:
            q_status = "CRIT"
            q_label = f"CRITIQUE · {tpm_pct}% · Quota Cloud Saturé"
        elif tpm_pct >= 70:
            q_status = "WARN"
            q_label = f"ALERTE · {tpm_pct}% · Alerte Quota Cloud"
        else:
            q_status = "OK"
            q_label = f"OK · {tpm_pct}% · Nominal Cloud Quota"

        in_tot = hm["cloud_in_total"]
        cached_tot = hm["cloud_cached_total"]
        denom = in_tot + cached_tot
        cache_hit_pct = round((cached_tot / denom) * 100) if denom > 0 else 0

        combined_windows = {
            k: int(oc["windows"][k]) + int(hm["windows"][k])
            for k in ("SESSION", "1H", "4H", "24H", "7J", "TOTAL")
        }
        local_windows = {
            k: int(oc["windows"][k]) + int(hm["local_windows"][k])
            for k in ("SESSION", "1H", "4H", "24H", "7J", "TOTAL")
        }
        local_cumul_windows = {
            "1H": int(hm["local_cumul_windows"]["1H"]) + int(oc["local_cumul_windows"]["1H"]),
            "4H": int(hm["local_cumul_windows"]["4H"]) + int(oc["local_cumul_windows"]["4H"]),
            "24H": int(hm["local_cumul_windows"]["24H"]) + int(oc["local_cumul_windows"]["24H"]),
            "sessions_1H": int(hm["local_window_sessions"]["1H"]) + int(oc["local_window_sessions"]["1H"]),
            "sessions_4H": int(hm["local_window_sessions"]["4H"]) + int(oc["local_window_sessions"]["4H"]),
            "sessions_24H": int(hm["local_window_sessions"]["24H"]) + int(oc["local_window_sessions"]["24H"]),
        }

        cloud_sessions = list(hm.get("cloud_sessions") or []) + list(oc.get("cloud_sessions") or [])
        status_rank = {"ACTIVE": 0, "OPEN": 1, "ENDED": 1}
        cloud_sessions.sort(key=lambda s: (status_rank.get(s.get("status", "ENDED"), 9), -float(s.get("updated_at") or 0.0)))
        cloud_sessions = cloud_sessions[:max_sessions]
        local_sessions = list(oc.get("sessions") or []) + list(hm.get("local_sessions") or [])
        local_sessions.sort(key=lambda s: (status_rank.get(s.get("status", "ENDED"), 9), -float(s.get("updated_at") or 0.0)))

        return {
            "active_topology": active_topology,
            "opencode": oc,
            "hermes": hm,
            "openwebui": ow,
            "time_windows": combined_windows,
            "local_time_windows": local_windows,
            "local_cumul_windows": local_cumul_windows,
            "local_sessions": local_sessions[:max_sessions],
            "cloud_quotas": {
                "status": q_status,
                "status_label": q_label,
                "focus_model": focus_model,
                "primary_model": primary_model,
                "active_profile": hm.get("active_profile", "default"),
                "include_all_profiles": bool(hm.get("include_all_profiles", True)),
                "available_profiles": hm.get("available_profiles", []),
                "by_profile": hm.get("by_profile", []),
                "tpm_sliding_60s": tpm_60s,
                "tpm_ceiling": tpm_ceil,
                "tpm_pct": tpm_pct,
                "tpm_headroom": tpm_free,
                "rpm_current": rpm_cur,
                "rpm_ceiling": rpm_ceil,
                "rpd_current": rpd_cur,
                "rpd_ceiling": rpd_ceil,
                "global_tpm_60s": hm["cloud_tpm_60s"],
                "global_rpm_60s": hm["cloud_rpm_60s"],
                "global_rpd_24h": hm["cloud_rpd_24h"],
                "cumul_windows": {
                    "1H": int(hm["cloud_windows"]["1H"]) + int(oc["cloud_windows"]["1H"]),
                    "4H": int(hm["cloud_windows"]["4H"]) + int(oc["cloud_windows"]["4H"]),
                    "24H": int(hm["cloud_windows"]["24H"]) + int(oc["cloud_windows"]["24H"]),
                    "sessions_1H": int(hm["cloud_window_sessions"]["1H"]) + int(oc["cloud_window_sessions"]["1H"]),
                    "sessions_4H": int(hm["cloud_window_sessions"]["4H"]) + int(oc["cloud_window_sessions"]["4H"]),
                    "sessions_24H": int(hm["cloud_window_sessions"]["24H"]) + int(oc["cloud_window_sessions"]["24H"]),
                },
                "cache_hit_pct": cache_hit_pct,
                "cached_tokens": cached_tot,
                "total_in_tokens": in_tot,
                "total_out_tokens": hm["cloud_out_total"],
                "total_rsn_tokens": hm["cloud_rsn_total"],
                "active_task_title": cloud_sess.get("title") or "Session Cloud active",
                "last_user_prompt": cloud_sess.get("last_prompt") or "",
                "tool_calls": int(cloud_sess.get("tool_calls", 0)),
                "api_calls": int(cloud_sess.get("api_calls", 0)),
                "profile_name": cloud_sess.get("profile") or hm.get("active_profile", "default"),
                "cwd": cloud_sess.get("cwd") or "",
                "by_model": by_model,
                "sessions": cloud_sessions,
            },
        }
