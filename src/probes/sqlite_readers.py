"""
Sonde SQLite Read-Only : OpenCode, Hermes Agent & OpenWebUI.
Ouverture stricte en lecture seule (?mode=ro) via sqlite3.
- Scanne ~/.hermes/state.db et ~/.hermes/profiles/*/state.db (hors backups/snapshots)
- Scanne ~/.local/share/opencode/opencode.db
- Scanne ~/.local/share/open-webui/webui.db (optionnel, dégradation gracieuse)
Calcule les fenêtres temporelles (SESSION, 1H, 24H, 7J, TOTAL) et le TPM/RPM/RPD Cloud (60s).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class SqliteTelemetryReader:
    def __init__(self, tpm_ceiling: int = 2_000_000, rpm_ceiling: int = 1000, rpd_ceiling: int = 10000) -> None:
        self.tpm_ceiling = tpm_ceiling
        self.rpm_ceiling = rpm_ceiling
        self.rpd_ceiling = rpd_ceiling

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
        # OpenCode stocke en millisecondes (~1.79e12), Hermes en secondes (~1.79e9)
        return val / 1000.0 if val > 1e12 else val

    @staticmethod
    def _parse_opencode_model(raw_model: Any) -> str:
        if not raw_model:
            return "Unknown"
        if isinstance(raw_model, str) and raw_model.startswith("{"):
            try:
                obj = json.loads(raw_model)
                return str(obj.get("id") or raw_model)
            except json.JSONDecodeError:
                return raw_model
        return str(raw_model)

    def _read_opencode_db(self, now: float) -> Dict[str, Any]:
        db_path = Path.home() / ".local/share/opencode/opencode.db"
        conn = self._connect_ro(db_path)
        result: Dict[str, Any] = {
            "available": False,
            "latest_ts": 0.0,
            "latest_session": None,
            "windows": {"SESSION": 0, "1H": 0, "24H": 0, "7J": 0, "TOTAL": 0},
        }
        if conn is None:
            return result

        try:
            cur = conn.cursor()
            cols = {r[1] for r in cur.execute("PRAGMA table_info(session)").fetchall()}
            if not cols:
                return result
            result["available"] = True
            rows = cur.execute(
                f"""
                SELECT id, title, directory, time_created, time_updated, model,
                       tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write,
                       {'agent' if 'agent' in cols else "'build' AS agent"}
                FROM session
                ORDER BY COALESCE(time_updated, time_created) DESC
                LIMIT 200
                """
            ).fetchall()

            for idx, r in enumerate(rows):
                sid, title, directory, t_cre, t_upd, model_raw, t_in, t_out, t_rsn, t_cr, t_cw, agent_raw = r
                ts = self._norm_ts(t_upd or t_cre)
                tok_sum = int(t_in or 0) + int(t_out or 0) + int(t_rsn or 0) + int(t_cr or 0)
                if idx == 0:
                    tool_cnt = 0
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
                    result["latest_ts"] = ts
                    result["latest_session"] = {
                        "id": sid,
                        "title": title or "OpenCode Session",
                        "directory": directory or "",
                        "model": self._parse_opencode_model(model_raw),
                        "agent": str(agent_raw or "build").upper(),
                        "tool_calls": tool_cnt,
                        "tokens_in": int(t_in or 0),
                        "tokens_out": int(t_out or 0),
                        "tokens_rsn": int(t_rsn or 0),
                        "tokens_cache_read": int(t_cr or 0),
                        "tokens_total": tok_sum,
                        "updated_at": ts,
                    }
                    result["windows"]["SESSION"] = tok_sum
                age = max(0.0, now - ts)
                if age <= 3600:
                    result["windows"]["1H"] += tok_sum
                if age <= 86400:
                    result["windows"]["24H"] += tok_sum
                if age <= 7 * 86400:
                    result["windows"]["7J"] += tok_sum
                result["windows"]["TOTAL"] += tok_sum
        except sqlite3.Error:
            pass
        finally:
            conn.close()
        return result

    def _iter_hermes_dbs(self) -> List[Path]:
        home = Path.home()
        candidates: List[Path] = []
        root_db = home / ".hermes/state.db"
        if root_db.is_file():
            candidates.append(root_db)
        profiles_dir = home / ".hermes/profiles"
        if profiles_dir.is_dir():
            for prof in sorted(profiles_dir.iterdir()):
                db = prof / "state.db"
                if prof.is_dir() and db.is_file():
                    candidates.append(db)
        return candidates

    def _read_hermes_dbs(self, now: float) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "available": False,
            "latest_ts": 0.0,
            "latest_session": None,
            "latest_cloud_ts": 0.0,
            "latest_cloud_session": None,
            "latest_local_ts": 0.0,
            "latest_local_session": None,
            "windows": {"SESSION": 0, "1H": 0, "24H": 0, "7J": 0, "TOTAL": 0},
            "local_windows": {"SESSION": 0, "1H": 0, "24H": 0, "7J": 0, "TOTAL": 0},
            "cloud_tpm_60s": 0,
            "cloud_rpm_60s": 0,
            "cloud_rpd_24h": 0,
            "cloud_in_total": 0,
            "cloud_out_total": 0,
            "cloud_rsn_total": 0,
            "cloud_cached_total": 0,
        }
        best_cloud_db: Optional[Path] = None
        best_cloud_sid: Optional[str] = None
        dbs = self._iter_hermes_dbs()
        for db_path in dbs:
            conn = self._connect_ro(db_path)
            if conn is None:
                continue
            prof_default = db_path.parent.name if db_path.parent.parent.name == "profiles" else "default"
            try:
                cur = conn.cursor()
                cols = {r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()}
                if not cols:
                    continue
                result["available"] = True
                prof_sel = "profile_name" if "profile_name" in cols else "NULL AS profile_name"
                rows = cur.execute(
                    f"""
                    SELECT id, model, title, tool_call_count, input_tokens, output_tokens,
                           cache_read_tokens, reasoning_tokens, last_activity_at, started_at,
                           cwd, billing_provider, api_call_count, {prof_sel}
                    FROM sessions
                    ORDER BY COALESCE(last_activity_at, started_at) DESC
                    LIMIT 200
                    """
                ).fetchall()

                for r in rows:
                    (
                        sid, model, title, tools_cnt, t_in, t_out, t_cr, t_rsn,
                        last_act, started, cwd, provider, api_calls, prof_name,
                    ) = r
                    ts = self._norm_ts(last_act or started)
                    in_v = int(t_in or 0)
                    out_v = int(t_out or 0)
                    cr_v = int(t_cr or 0)
                    rsn_v = int(t_rsn or 0)
                    tok_sum = in_v + out_v + rsn_v + cr_v
                    prof_label = str(prof_name or prof_default)

                    if ts > result["latest_ts"]:
                        result["latest_ts"] = ts
                        result["latest_session"] = {
                            "id": sid,
                            "model": model or "Cloud Model",
                            "title": title or "Hermes Session",
                            "tool_calls": int(tools_cnt or 0),
                            "api_calls": int(api_calls or 0),
                            "profile": prof_label,
                            "tokens_in": in_v,
                            "tokens_out": out_v,
                            "tokens_rsn": rsn_v,
                            "tokens_cache_read": cr_v,
                            "tokens_total": tok_sum,
                            "cwd": cwd or "",
                            "provider": provider or "cloud",
                            "updated_at": ts,
                        }
                        result["windows"]["SESSION"] = tok_sum

                    age = max(0.0, now - ts)
                    if age <= 3600:
                        result["windows"]["1H"] += tok_sum
                    if age <= 86400:
                        result["windows"]["24H"] += tok_sum
                    if age <= 7 * 86400:
                        result["windows"]["7J"] += tok_sum
                    result["windows"]["TOTAL"] += tok_sum

                    prov_low = str(provider or "").lower()
                    is_cloud = prov_low not in {"local", "lemonade", "llamacpp", "vllm"}
                    if is_cloud:
                        if ts > result["latest_cloud_ts"]:
                            result["latest_cloud_ts"] = ts
                            best_cloud_db = db_path
                            best_cloud_sid = str(sid)
                            result["latest_cloud_session"] = {
                                "id": sid,
                                "model": model or "Cloud Model",
                                "title": title or "Hermes Cloud Session",
                                "tool_calls": int(tools_cnt or 0),
                                "api_calls": int(api_calls or 0),
                                "profile": prof_label,
                                "cwd": cwd or "",
                                "provider": provider or "cloud",
                                "last_prompt": "",
                                "updated_at": ts,
                            }
                        if age <= 86400:
                            result["cloud_rpd_24h"] += int(api_calls or 1)
                            result["cloud_in_total"] += in_v
                            result["cloud_out_total"] += out_v
                            result["cloud_rsn_total"] += rsn_v
                            result["cloud_cached_total"] += cr_v
                    else:
                        if ts > result["latest_local_ts"]:
                            result["latest_local_ts"] = ts
                            result["latest_local_session"] = {
                                "id": sid,
                                "model": model or "Local Model",
                                "title": title or "Hermes Local Session",
                                "tool_calls": int(tools_cnt or 0),
                                "tokens_in": in_v,
                                "tokens_out": out_v,
                                "tokens_rsn": rsn_v,
                                "tokens_cache_read": cr_v,
                                "tokens_total": tok_sum,
                                "cwd": cwd or "",
                                "provider": provider or "local",
                                "agent": "HERMES LOCAL",
                                "updated_at": ts,
                            }
                            result["local_windows"]["SESSION"] = tok_sum
                        if age <= 3600:
                            result["local_windows"]["1H"] += tok_sum
                        if age <= 86400:
                            result["local_windows"]["24H"] += tok_sum
                        if age <= 7 * 86400:
                            result["local_windows"]["7J"] += tok_sum
                        result["local_windows"]["TOTAL"] += tok_sum

                # Calcul glissant strict 60s sur les messages récents des sessions cloud
                msg_cols = {r[1] for r in cur.execute("PRAGMA table_info(messages)").fetchall()}
                if "timestamp" in msg_cols and "token_count" in msg_cols:
                    cutoff_60s = now - 60.0
                    m_rows = cur.execute(
                        """
                        SELECT m.timestamp, m.token_count, s.billing_provider,
                               s.input_tokens, s.output_tokens, s.reasoning_tokens, s.api_call_count
                        FROM messages m
                        JOIN sessions s ON m.session_id = s.id
                        WHERE m.timestamp >= ?
                        """,
                        (cutoff_60s,),
                    ).fetchall()
                    for mts, mtok, sprov, s_in, s_out, s_rsn, s_calls in m_rows:
                        if str(sprov or "").lower() not in {"local", "lemonade", "llamacpp", "vllm"}:
                            result["cloud_rpm_60s"] += 1
                            if mtok is not None and int(mtok) > 0:
                                result["cloud_tpm_60s"] += int(mtok)
                            else:
                                avg_call_tok = (int(s_in or 0) + int(s_out or 0) + int(s_rsn or 0)) // max(1, int(s_calls or 1))
                                result["cloud_tpm_60s"] += avg_call_tok
            except sqlite3.Error:
                pass
            finally:
                conn.close()

        # Dernière requête utilisateur réelle de la session Cloud gagnante
        if best_cloud_db is not None and best_cloud_sid and result["latest_cloud_session"]:
            conn = self._connect_ro(best_cloud_db)
            if conn is not None:
                try:
                    row = conn.execute(
                        "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY timestamp DESC, id DESC LIMIT 1",
                        (best_cloud_sid,),
                    ).fetchone()
                    if row and row[0]:
                        txt = str(row[0]).split("--- Attached Context ---")[0]
                        txt = re.sub(r"@file:\S+\s*", "", txt)
                        txt = re.sub(r"\s+", " ", txt).strip()
                        if len(txt) > 115:
                            txt = txt[:112].rstrip() + "..."
                        result["latest_cloud_session"]["last_prompt"] = txt
                except sqlite3.Error:
                    pass
                finally:
                    conn.close()
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
                requests.append({
                    "id": f"REQ #{idx}",
                    "user": str(uname),
                    "prompt": str(title or "Chat OpenWebUI")[:48],
                    "in_tok": "--",
                    "out_tok": "--",
                    "speed": "--",
                })
        except sqlite3.Error:
            pass
        finally:
            conn.close()
        return {"available": True, "latest_ts": latest_ts, "requests": requests}

    def read_active_topology_and_tokens(self) -> Dict[str, Any]:
        """Lire les bases SQLite en ?mode=ro et calculer la fenêtre glissante 60s."""
        now = time.time()
        oc = self._read_opencode_db(now)
        hm = self._read_hermes_dbs(now)
        ow = self._read_openwebui_db()

        oc_ts = oc["latest_ts"]
        hm_ts = hm["latest_ts"]
        ow_ts = ow["latest_ts"]

        # Arbitrage topologie active (1: Hermes+OpenCode+Vulkan, 2: OpenCode Direct, 3: OpenWebUI+vLLM, 4: Hermes Cloud)
        if ow_ts > max(oc_ts, hm_ts) and ow["requests"]:
            active_topology = 3
        elif oc_ts > 0 and hm_ts > 0 and abs(hm_ts - oc_ts) < 900:
            active_topology = 1
        elif oc_ts > hm_ts:
            active_topology = 2
        elif hm_ts > 0:
            active_topology = 4
        else:
            active_topology = 1

        tpm_60s = hm["cloud_tpm_60s"]
        tpm_pct = min(100, round((tpm_60s / max(1, self.tpm_ceiling)) * 100))
        tpm_free = max(0, self.tpm_ceiling - tpm_60s)
        if tpm_pct >= 85:
            q_status = "CRIT"
            q_label = f"CRIT : OUT OF QUOTA IMMINENT ({tpm_pct}%)"
        elif tpm_pct >= 70:
            q_status = "WARN"
            q_label = f"WARN : RISQUE OUT OF QUOTA ({tpm_pct}%)"
        else:
            q_status = "OK"
            q_label = f"OK : QUOTA CLOUD NOMINAL ({tpm_pct}%)"

        in_tot = hm["cloud_in_total"]
        cached_tot = hm["cloud_cached_total"]
        denom = in_tot + cached_tot
        cache_hit_pct = round((cached_tot / denom) * 100) if denom > 0 else 0

        # Fenêtres combinées (global) + fenêtres strictement locales (OpenCode + Hermes local)
        combined_windows = {
            k: int(oc["windows"][k]) + int(hm["windows"][k])
            for k in ("SESSION", "1H", "24H", "7J", "TOTAL")
        }
        local_windows = {
            k: int(oc["windows"][k]) + int(hm["local_windows"][k])
            for k in ("SESSION", "1H", "24H", "7J", "TOTAL")
        }

        cloud_sess = hm.get("latest_cloud_session") or hm.get("latest_session") or {}

        return {
            "active_topology": active_topology,
            "opencode": oc,
            "hermes": hm,
            "openwebui": ow,
            "time_windows": combined_windows,
            "local_time_windows": local_windows,
            "cloud_quotas": {
                "status": q_status,
                "status_label": q_label,
                "tpm_sliding_60s": tpm_60s,
                "tpm_ceiling": self.tpm_ceiling,
                "tpm_pct": tpm_pct,
                "tpm_headroom": tpm_free,
                "rpm_current": hm["cloud_rpm_60s"],
                "rpm_ceiling": self.rpm_ceiling,
                "rpd_current": hm["cloud_rpd_24h"],
                "rpd_ceiling": self.rpd_ceiling,
                "cache_hit_pct": cache_hit_pct,
                "cached_tokens": cached_tot,
                "total_in_tokens": in_tot,
                "total_out_tokens": hm["cloud_out_total"],
                "total_rsn_tokens": hm["cloud_rsn_total"],
                "active_task_title": cloud_sess.get("title") or "Session Cloud active",
                "last_user_prompt": cloud_sess.get("last_prompt") or "",
                "tool_calls": int(cloud_sess.get("tool_calls", 0)),
                "api_calls": int(cloud_sess.get("api_calls", 0)),
                "profile_name": cloud_sess.get("profile") or "default",
                "cwd": cloud_sess.get("cwd") or "",
            },
        }
