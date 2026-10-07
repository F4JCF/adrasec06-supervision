"""Stockage local (SQLite) des nœuds, du journal, des mesures et des réglages."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "ADRASEC06-Supervision"


def data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def resource_path(name: str) -> Path:
    """Chemin d'un fichier embarqué (fonctionne aussi dans l'exécutable PyInstaller)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    p = base / name
    if not p.exists():
        p = Path(__file__).resolve().parent / name
    return p


class Store:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / "supervision.db"
        self._lock = threading.RLock()
        self._cx = sqlite3.connect(self.path, check_same_thread=False)
        self._cx.row_factory = sqlite3.Row
        self.revision = 0  # incrémenté à chaque modification, l'interface s'en sert pour se rafraîchir
        with self._lock:
            self._cx.executescript(
                """
                CREATE TABLE IF NOT EXISTS noeuds (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL, noeud TEXT, auteur TEXT, texte TEXT NOT NULL, auto INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS mesures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    noeud TEXT NOT NULL, date TEXT NOT NULL, ts REAL NOT NULL,
                    bruit REAL, erreurs INTEGER, bat_mv INTEGER, uptime INTEGER,
                    last_snr REAL, nb_recv INTEGER, nb_sent INTEGER);
                CREATE INDEX IF NOT EXISTS mesures_noeud ON mesures(noeud, ts);
                CREATE TABLE IF NOT EXISTS config (cle TEXT PRIMARY KEY, valeur TEXT);
                """
            )
            self._cx.commit()
        if not self.list_nodes():
            self._seed()

    # ---------- interne ----------
    def _bump(self):
        self.revision += 1

    def _seed(self):
        try:
            seed = json.loads(resource_path("seed.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for n in seed.get("noeuds", []):
            self.save_node(n, bump=False)
        for j in seed.get("journal", []):
            self.add_journal(j.get("noeud", ""), j.get("auteur", ""), j.get("texte", ""), date=j.get("date"), bump=False)
        self._bump()

    # ---------- nœuds ----------
    def list_nodes(self) -> list[dict]:
        with self._lock:
            rows = self._cx.execute("SELECT id, data FROM noeuds").fetchall()
        out = []
        for r in rows:
            d = json.loads(r["data"])
            d["id"] = r["id"]
            out.append(d)
        return out

    def get_node(self, node_id: str) -> dict | None:
        with self._lock:
            r = self._cx.execute("SELECT data FROM noeuds WHERE id=?", (node_id,)).fetchone()
        if not r:
            return None
        d = json.loads(r["data"])
        d["id"] = node_id
        return d

    def find_by_name(self, name: str) -> dict | None:
        low = (name or "").strip().lower()
        for n in self.list_nodes():
            if (n.get("nom") or "").strip().lower() == low:
                return n
        return None

    def save_node(self, node: dict, bump: bool = True) -> dict:
        node = dict(node)
        node_id = node.pop("id", None) or slug(node.get("nom", ""))
        with self._lock:
            self._cx.execute(
                "INSERT INTO noeuds(id, data) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (node_id, json.dumps(node, ensure_ascii=False)),
            )
            self._cx.commit()
        if bump:
            self._bump()
        node["id"] = node_id
        return node

    def patch_node(self, node_id: str, fields: dict) -> dict | None:
        n = self.get_node(node_id)
        if n is None:
            return None
        n.update(fields)
        return self.save_node(n)

    def delete_node(self, node_id: str):
        with self._lock:
            self._cx.execute("DELETE FROM noeuds WHERE id=?", (node_id,))
            self._cx.execute("DELETE FROM config WHERE cle=?", ("mdp:" + node_id,))
            self._cx.commit()
        self._bump()

    # ---------- journal ----------
    def add_journal(self, noeud: str, auteur: str, texte: str, date: str | None = None, auto: bool = False, bump: bool = True):
        with self._lock:
            self._cx.execute(
                "INSERT INTO journal(date, noeud, auteur, texte, auto) VALUES(?,?,?,?,?)",
                (date or now_iso(), noeud or "", auteur or "", texte, 1 if auto else 0),
            )
            self._cx.commit()
        if bump:
            self._bump()

    def list_journal(self, limit: int = 300) -> list[dict]:
        with self._lock:
            rows = self._cx.execute(
                "SELECT id, date, noeud, auteur, texte, auto FROM journal ORDER BY date DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_journal(self, entry_id: int):
        with self._lock:
            self._cx.execute("DELETE FROM journal WHERE id=?", (entry_id,))
            self._cx.commit()
        self._bump()

    # ---------- mesures ----------
    def add_measure(self, noeud: str, m: dict):
        with self._lock:
            self._cx.execute(
                "INSERT INTO mesures(noeud, date, ts, bruit, erreurs, bat_mv, uptime, last_snr, nb_recv, nb_sent)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (noeud, now_iso(), time.time(), m.get("noise_floor"), m.get("recv_errors"), m.get("bat"),
                 m.get("uptime"), m.get("last_snr"), m.get("nb_recv"), m.get("nb_sent")),
            )
            # on garde 90 jours d'historique
            self._cx.execute("DELETE FROM mesures WHERE ts < ?", (time.time() - 90 * 86400,))
            self._cx.commit()

    def history(self, noeud: str, days: int = 7) -> list[dict]:
        with self._lock:
            rows = self._cx.execute(
                "SELECT date, ts, bruit, erreurs, bat_mv, uptime, last_snr, nb_recv FROM mesures"
                " WHERE noeud=? AND ts >= ? ORDER BY ts", (noeud, time.time() - days * 86400)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- réglages ----------
    def get_config(self, key: str, default=None):
        with self._lock:
            r = self._cx.execute("SELECT valeur FROM config WHERE cle=?", (key,)).fetchone()
        if r is None:
            return default
        try:
            return json.loads(r["valeur"])
        except ValueError:
            return r["valeur"]

    def set_config(self, key: str, value):
        with self._lock:
            self._cx.execute(
                "INSERT INTO config(cle, valeur) VALUES(?, ?) ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self._cx.commit()

    def password(self, node_id: str) -> str:
        return self.get_config("mdp:" + node_id, "") or ""

    def set_password(self, node_id: str, pwd: str):
        self.set_config("mdp:" + node_id, pwd or "")
        self._bump()

    def has_password(self, node_id: str) -> bool:
        return bool(self.password(node_id))

    # ---------- export / import ----------
    def export_data(self) -> dict:
        return {"format": "adrasec06-supervision", "version": 1, "exporte": now_iso(),
                "noeuds": self.list_nodes(), "journal": self.list_journal(10000)}

    def import_data(self, data: dict) -> tuple[int, int]:
        if not isinstance(data, dict) or not isinstance(data.get("noeuds"), list):
            raise ValueError("Ce fichier n'est pas un export de la supervision ADRASEC 06.")
        n_nodes = 0
        for n in data["noeuds"]:
            if isinstance(n, dict) and n.get("nom"):
                self.save_node(n, bump=False)
                n_nodes += 1
        existing = {(j["date"], j["texte"]) for j in self.list_journal(100000)}
        n_j = 0
        for j in data.get("journal", []):
            if isinstance(j, dict) and j.get("texte") and (j.get("date"), j["texte"]) not in existing:
                self.add_journal(j.get("noeud", ""), j.get("auteur", ""), j["texte"], date=j.get("date"),
                                 auto=bool(j.get("auto")), bump=False)
                n_j += 1
        self._bump()
        return n_nodes, n_j


def slug(s: str) -> str:
    import re
    import unicodedata
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-")[:120]
    return s or f"n-{int(time.time() * 1000)}"
