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
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL, ts REAL NOT NULL, sens TEXT NOT NULL,
                    canal INTEGER, canal_nom TEXT, contact TEXT, contact_cle TEXT,
                    auteur TEXT, texte TEXT NOT NULL, snr REAL, sauts INTEGER, statut TEXT);
                CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
                CREATE TABLE IF NOT EXISTS exercices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nom TEXT NOT NULL, type TEXT, debut TEXT NOT NULL, fin TEXT, responsable TEXT);
                CREATE TABLE IF NOT EXISTS essais (noeud TEXT NOT NULL, ts REAL NOT NULL, ok INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS essais_noeud ON essais(noeud, ts);
                CREATE TABLE IF NOT EXISTS positions (cle TEXT NOT NULL, nom TEXT, ts REAL NOT NULL, lat REAL, lon REAL);
                CREATE INDEX IF NOT EXISTS positions_cle ON positions(cle, ts);
                CREATE TABLE IF NOT EXISTS couverture (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, lat REAL, lon REAL, lieu TEXT, resultats TEXT);
                CREATE TABLE IF NOT EXISTS alertes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL, niveau TEXT NOT NULL, noeud TEXT, texte TEXT NOT NULL, vue INTEGER DEFAULT 0);
                """
            )
            cols = {r[1] for r in self._cx.execute("PRAGMA table_info(journal)")}
            if "exercice" not in cols:
                self._cx.execute("ALTER TABLE journal ADD COLUMN exercice INTEGER")
            # main courante au format sécurité civile
            for col, typ in (("numero", "INTEGER"), ("emetteur", "TEXT"), ("destinataire", "TEXT"), ("nature", "TEXT"),
                             ("suite", "TEXT"), ("cloture", "TEXT")):
                if col not in cols:
                    self._cx.execute(f"ALTER TABLE journal ADD COLUMN {col} {typ}")
            if "numero" not in cols:  # numérotation des mains courantes existantes
                for (ex_id,) in self._cx.execute("SELECT DISTINCT exercice FROM journal WHERE exercice IS NOT NULL").fetchall():
                    ids = [r[0] for r in self._cx.execute("SELECT id FROM journal WHERE exercice=? ORDER BY date, id", (ex_id,))]
                    for i, jid in enumerate(ids, 1):
                        self._cx.execute("UPDATE journal SET numero=?, emetteur=COALESCE(emetteur, auteur),"
                                         " nature=COALESCE(nature, CASE WHEN auto=1 OR UPPER(auteur)='AUTO' THEN 'evenement' ELSE 'info' END)"
                                         " WHERE id=?", (i, jid))
            ecols = {r[1] for r in self._cx.execute("PRAGMA table_info(exercices)")}
            for col in ("lieu", "autorite", "description", "bilan", "signataire"):
                if col not in ecols:
                    self._cx.execute(f"ALTER TABLE exercices ADD COLUMN {col} TEXT")
            self._cx.execute("""CREATE TABLE IF NOT EXISTS moyens (
                id INTEGER PRIMARY KEY AUTOINCREMENT, exercice INTEGER NOT NULL, indicatif TEXT, nom TEXT,
                fonction TEXT, equipe TEXT, materiel TEXT, lieu TEXT, contact TEXT, arrivee TEXT, depart TEXT)""")
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
    def add_journal(self, noeud: str, auteur: str, texte: str, date: str | None = None, auto: bool = False,
                    bump: bool = True, exercice: int | None = -1, emetteur: str | None = None,
                    destinataire: str | None = None, nature: str | None = None) -> int:
        if exercice == -1:  # par défaut : rattachée à l'exercice en cours, s'il y en a un
            ex = self.current_exercise()
            exercice = ex["id"] if ex else None
        if nature is None:
            nature = "evenement" if auto or (auteur or "").upper() == "AUTO" else "info"
        with self._lock:
            numero = None
            if exercice:
                r = self._cx.execute("SELECT COALESCE(MAX(numero), 0) FROM journal WHERE exercice=?", (exercice,)).fetchone()
                numero = (r[0] or 0) + 1
            c = self._cx.execute(
                "INSERT INTO journal(date, noeud, auteur, texte, auto, exercice, numero, emetteur, destinataire, nature)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (date or now_iso(), noeud or "", auteur or "", texte, 1 if auto else 0, exercice, numero,
                 emetteur if emetteur is not None else (auteur or ""), destinataire or "", nature),
            )
            self._cx.commit()
            jid = c.lastrowid
        if bump:
            self._bump()
        return jid

    def update_journal(self, jid: int, fields: dict):
        allowed = {k: v for k, v in fields.items() if k in ("suite", "cloture", "texte", "emetteur", "destinataire", "nature", "date")}
        if not allowed:
            return
        with self._lock:
            self._cx.execute(f"UPDATE journal SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), jid))
            self._cx.commit()
        self._bump()

    def list_journal(self, limit: int = 300) -> list[dict]:
        with self._lock:
            rows = self._cx.execute(
                "SELECT id, date, noeud, auteur, texte, auto, exercice FROM journal ORDER BY date DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def journal_of_exercise(self, ex_id: int) -> list[dict]:
        with self._lock:
            rows = self._cx.execute(
                "SELECT id, date, noeud, auteur, texte, auto, numero, emetteur, destinataire, nature, suite, cloture"
                " FROM journal WHERE exercice=? ORDER BY numero, date, id", (ex_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- exercices / interventions ----------
    def current_exercise(self) -> dict | None:
        with self._lock:
            r = self._cx.execute("SELECT * FROM exercices WHERE fin IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def start_exercise(self, nom: str, type_: str, responsable: str, lieu: str = "", autorite: str = "",
                       description: str = "") -> dict:
        cur = self.current_exercise()
        if cur:
            self.stop_exercise()
        with self._lock:
            c = self._cx.execute("INSERT INTO exercices(nom, type, debut, responsable, lieu, autorite, description)"
                                 " VALUES(?,?,?,?,?,?,?)", (nom, type_, now_iso(), responsable, lieu, autorite, description))
            self._cx.commit()
            ex_id = c.lastrowid
        self.add_journal("", responsable or "PC", f"Ouverture de la main courante : {type_ or 'exercice'} « {nom} »"
                         + (f", {lieu}" if lieu else "") + (f", à la demande de {autorite}" if autorite else "") + ".",
                         exercice=ex_id, nature="evenement")
        return self.get_exercise(ex_id)

    def update_exercise(self, ex_id: int, fields: dict):
        allowed = {k: v for k, v in fields.items()
                   if k in ("nom", "type", "lieu", "autorite", "description", "bilan", "responsable", "signataire")}
        if not allowed:
            return
        with self._lock:
            self._cx.execute(f"UPDATE exercices SET {', '.join(k + '=?' for k in allowed)} WHERE id=?",
                             (*allowed.values(), ex_id))
            self._cx.commit()
        self._bump()

    def stop_exercise(self, bilan: str | None = None) -> dict | None:
        cur = self.current_exercise()
        if not cur:
            return None
        if bilan is not None:
            self.update_exercise(cur["id"], {"bilan": bilan})
        # les moyens encore présents sont notés partis à la clôture
        for m in self.list_moyens(cur["id"]):
            if m.get("arrivee") and not m.get("depart"):
                self.update_moyen(m["id"], {"depart": now_iso()}, journal=False)
        self.add_journal("", cur.get("responsable") or "PC", f"Clôture de la main courante : {cur['nom']}.",
                         exercice=cur["id"], nature="evenement")
        with self._lock:
            self._cx.execute("UPDATE exercices SET fin=? WHERE id=?", (now_iso(), cur["id"]))
            self._cx.commit()
        self._bump()
        return self.get_exercise(cur["id"])

    def get_exercise(self, ex_id: int) -> dict | None:
        with self._lock:
            r = self._cx.execute("SELECT * FROM exercices WHERE id=?", (ex_id,)).fetchone()
        return dict(r) if r else None

    def list_exercises(self) -> list[dict]:
        with self._lock:
            rows = self._cx.execute("SELECT * FROM exercices ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    # ---------- messagerie ----------
    def add_message(self, m: dict) -> int:
        with self._lock:
            c = self._cx.execute(
                "INSERT INTO messages(date, ts, sens, canal, canal_nom, contact, contact_cle, auteur, texte, snr, sauts, statut)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (m.get("date") or now_iso(), m.get("ts") or time.time(), m["sens"], m.get("canal"), m.get("canal_nom"),
                 m.get("contact"), m.get("contact_cle"), m.get("auteur"), m["texte"], m.get("snr"), m.get("sauts"),
                 m.get("statut")))
            self._cx.execute("DELETE FROM messages WHERE ts < ?", (time.time() - 365 * 86400,))
            self._cx.commit()
            mid = c.lastrowid
        self._bump()
        return mid

    def set_message_status(self, mid: int, statut: str):
        with self._lock:
            self._cx.execute("UPDATE messages SET statut=? WHERE id=?", (statut, mid))
            self._cx.commit()
        self._bump()

    def list_messages(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._cx.execute("SELECT * FROM messages ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows][::-1]

    # ---------- alertes ----------
    def add_alert(self, niveau: str, noeud: str, texte: str):
        with self._lock:
            self._cx.execute("INSERT INTO alertes(date, niveau, noeud, texte) VALUES(?,?,?,?)",
                             (now_iso(), niveau, noeud, texte))
            self._cx.execute("DELETE FROM alertes WHERE id NOT IN (SELECT id FROM alertes ORDER BY id DESC LIMIT 500)")
            self._cx.commit()
        self._bump()

    def list_alerts(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._cx.execute("SELECT * FROM alertes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_alerts_seen(self):
        with self._lock:
            self._cx.execute("UPDATE alertes SET vue=1 WHERE vue=0")
            self._cx.commit()
        self._bump()

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


# ---------- disponibilité, positions, couverture, sauvegarde ----------
def _attach():
    def add_attempt(self, noeud: str, ok: bool):
        with self._lock:
            self._cx.execute("INSERT INTO essais(noeud, ts, ok) VALUES(?,?,?)", (noeud, time.time(), 1 if ok else 0))
            self._cx.execute("DELETE FROM essais WHERE ts < ?", (time.time() - 180 * 86400,))
            self._cx.commit()

    def availability(self, noeud: str, days: int = 30) -> dict:
        since = time.time() - days * 86400
        with self._lock:
            rows = self._cx.execute("SELECT ts, ok FROM essais WHERE noeud=? AND ts>=? ORDER BY ts", (noeud, since)).fetchall()
        total = len(rows)
        ok = sum(r["ok"] for r in rows)
        coupures, debut, n_fail = [], None, 0
        for r in rows:
            if not r["ok"]:
                if debut is None:
                    debut, n_fail = r["ts"], 0
                n_fail += 1
            elif debut is not None:
                coupures.append({"debut": debut, "fin": r["ts"], "essais": n_fail})
                debut = None
        if debut is not None:
            coupures.append({"debut": debut, "fin": None, "essais": n_fail})
        return {"pct": round(100 * ok / total, 1) if total else None, "essais": total,
                "coupures": coupures[-20:][::-1]}

    def add_position(self, cle: str, nom: str, lat: float, lon: float, ts: float | None = None) -> bool:
        ts = ts or time.time()
        with self._lock:
            last = self._cx.execute("SELECT ts, lat, lon FROM positions WHERE cle=? ORDER BY ts DESC LIMIT 1", (cle,)).fetchone()
            if last and abs(last["lat"] - lat) < 0.0002 and abs(last["lon"] - lon) < 0.0002:
                return False
            self._cx.execute("INSERT INTO positions(cle, nom, ts, lat, lon) VALUES(?,?,?,?,?)", (cle, nom, ts, lat, lon))
            self._cx.execute("DELETE FROM positions WHERE ts < ?", (time.time() - 30 * 86400,))
            self._cx.commit()
        return True

    def tracks(self, hours: float = 6) -> dict:
        with self._lock:
            rows = self._cx.execute("SELECT cle, ts, lat, lon FROM positions WHERE ts>=? ORDER BY ts",
                                    (time.time() - hours * 3600,)).fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(r["cle"], []).append([r["lat"], r["lon"], r["ts"]])
        return out

    def add_coverage(self, lat, lon, lieu: str, resultats: list) -> int:
        with self._lock:
            c = self._cx.execute("INSERT INTO couverture(date, lat, lon, lieu, resultats) VALUES(?,?,?,?,?)",
                                 (now_iso(), lat, lon, lieu, json.dumps(resultats, ensure_ascii=False)))
            self._cx.commit()
            cid = c.lastrowid
        self._bump()
        return cid

    def list_coverage(self) -> list[dict]:
        with self._lock:
            rows = self._cx.execute("SELECT * FROM couverture ORDER BY id DESC LIMIT 500").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["resultats"] = json.loads(d["resultats"] or "[]")
            out.append(d)
        return out

    def delete_coverage(self, cid: int):
        with self._lock:
            self._cx.execute("DELETE FROM couverture WHERE id=?", (cid,))
            self._cx.commit()
        self._bump()

    def backup(self, dossier: str, garder: int = 14) -> str:
        """Copie cohérente de la base + export JSON dans le dossier choisi ; garde les `garder` plus récentes."""
        d = Path(dossier)
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        dest = d / f"supervision-adrasec06_{stamp}.db"
        with self._lock:
            out = sqlite3.connect(dest)
            self._cx.backup(out)
            out.close()
        (d / f"supervision-adrasec06_{stamp}.json").write_text(
            json.dumps(self.export_data(), ensure_ascii=False, indent=1), encoding="utf-8")
        for ext in ("db", "json"):
            olds = sorted(d.glob(f"supervision-adrasec06_*.{ext}"))
            for o in olds[:-garder]:
                try:
                    o.unlink()
                except OSError:
                    pass
        self.set_config("derniere_sauvegarde", now_iso())
        self._bump()
        return str(dest)

    for f in (add_attempt, availability, add_position, tracks, add_coverage, list_coverage, delete_coverage, backup):
        setattr(Store, f.__name__, f)


_attach()


# ---------- moyens engagés ----------
def _attach_moyens():
    def list_moyens(self, ex_id: int) -> list[dict]:
        with self._lock:
            rows = self._cx.execute("SELECT * FROM moyens WHERE exercice=? ORDER BY id", (ex_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_moyen(self, ex_id: int, m: dict) -> int:
        keys = ("indicatif", "nom", "fonction", "equipe", "materiel", "lieu", "contact")
        with self._lock:
            c = self._cx.execute(f"INSERT INTO moyens(exercice, {', '.join(keys)}) VALUES(?{', ?' * len(keys)})",
                                 (ex_id, *[str(m.get(k) or "").strip() for k in keys]))
            self._cx.commit()
            mid = c.lastrowid
        self._bump()
        return mid

    def update_moyen(self, mid: int, fields: dict, journal: bool = True):
        allowed = {k: v for k, v in fields.items()
                   if k in ("indicatif", "nom", "fonction", "equipe", "materiel", "lieu", "contact", "arrivee", "depart")}
        if not allowed:
            return
        with self._lock:
            row = self._cx.execute("SELECT * FROM moyens WHERE id=?", (mid,)).fetchone()
            self._cx.execute(f"UPDATE moyens SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), mid))
            self._cx.commit()
        if row and journal:
            qui = row["indicatif"] or row["nom"] or "Moyen"
            if allowed.get("arrivee"):
                self.add_journal("", qui, f"{qui}{' (' + row['equipe'] + ')' if row['equipe'] else ''} arrivé"
                                 + (f" : {row['lieu']}" if row["lieu"] else "") + ".",
                                 exercice=row["exercice"], emetteur=qui, destinataire="PC", nature="compte-rendu")
            if allowed.get("depart"):
                self.add_journal("", qui, f"{qui} quitte la zone / désengagé.", exercice=row["exercice"],
                                 emetteur=qui, destinataire="PC", nature="compte-rendu")
        self._bump()

    def delete_moyen(self, mid: int):
        with self._lock:
            self._cx.execute("DELETE FROM moyens WHERE id=?", (mid,))
            self._cx.commit()
        self._bump()

    for f in (list_moyens, add_moyen, update_moyen, delete_moyen):
        setattr(Store, f.__name__, f)


_attach_moyens()
