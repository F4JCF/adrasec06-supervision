"""Test mensuel de résilience du réseau ADRASEC 06.

Envoie sur le canal ADRASEC 06 le message
« [EXERCICE ADRASEC 06 - TEST MENSUEL DE RESILIENCE - NE PAS REPONDRE - ACK AUTO] »,
interroge les répéteurs, puis écoute pendant 10 minutes :
  - les répéteurs qui relaient le message (échos entendus par le nœud companion),
  - les accusés automatiques (messages reçus sur le canal pendant le test),
  - l'état des répéteurs ADRASEC à la fin du test.
Le résultat est enregistré, noté au journal, mis en PDF (dossier des rapports) et envoyé par e-mail si demandé.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from .store import Store, now_iso

log = logging.getLogger("supervision")

TEXTE = "[EXERCICE ADRASEC 06 - TEST MENSUEL DE RESILIENCE - NE PAS REPONDRE - ACK AUTO]"
DUREE = 600  # secondes d'écoute


def _table(store: Store):
    store._cx.execute("""CREATE TABLE IF NOT EXISTS tests_resilience (
        id INTEGER PRIMARY KEY AUTOINCREMENT, debut REAL NOT NULL, fin REAL, texte TEXT, canal INTEGER,
        canal_nom TEXT, auto INTEGER DEFAULT 0, operateur TEXT, resultats TEXT, pdf TEXT)""")


class TestResilience:
    def __init__(self, store: Store, poller, api, alerte):
        self.store, self.poller, self.api, self.alerte = store, poller, api, alerte
        self._stop = threading.Event()
        self._fin_anticipee = threading.Event()
        self.courant = None  # id du test en cours
        with store._lock:
            _table(store)
            # un test resté ouvert (logiciel fermé pendant le test) est clos tel quel
            store._cx.execute("UPDATE tests_resilience SET fin=debut+? WHERE fin IS NULL", (DUREE,))
            store._cx.commit()
        threading.Thread(target=self._planificateur, name="test-resilience", daemon=True).start()

    # ---------- réglages ----------
    def reglages(self) -> dict:
        return {"auto": bool(self.store.get_config("test_auto", False)),
                "jour": int(self.store.get_config("test_jour", 1) or 1),
                "heure": self.store.get_config("test_heure", "20:00") or "20:00",
                "texte": TEXTE, "duree": DUREE}

    def regler(self, o: dict) -> dict:
        o = o or {}
        if "auto" in o:
            self.store.set_config("test_auto", bool(o["auto"]))
        if "jour" in o:
            try:
                self.store.set_config("test_jour", max(1, min(28, int(o["jour"]))))
            except (TypeError, ValueError):
                pass
        if "heure" in o:
            self.store.set_config("test_heure", str(o["heure"] or "20:00")[:5])
        self.store._bump()
        return {"ok": True, **self.reglages()}

    # ---------- données ----------
    def _get(self, tid: int) -> dict | None:
        with self.store._lock:
            r = self.store._cx.execute("SELECT * FROM tests_resilience WHERE id=?", (int(tid),)).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["resultats"] = json.loads(d.get("resultats") or "null")
        except ValueError:
            d["resultats"] = None
        return d

    def historique(self, n: int = 12) -> list[dict]:
        with self.store._lock:
            rows = self.store._cx.execute("SELECT id FROM tests_resilience ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [self._get(r[0]) for r in rows]

    def etat(self) -> dict:
        en_cours = None
        if self.courant:
            t = self._get(self.courant)
            if t and not t.get("fin"):
                en_cours = {**t, "resultats": self.resultats(t), "reste": max(0, int(t["debut"] + DUREE - time.time()))}
        return {"reglages": self.reglages(), "enCours": en_cours, "historique": self.historique()}

    def resultats(self, t: dict) -> dict:
        debut, fin = float(t["debut"]), float(t.get("fin") or time.time())
        moi = t.get("operateur_noeud") or self.poller.local_name or ""
        ech = self.api._resume_echos(f"{moi}: {t['texte']}", debut, fenetre=max(60, fin - debut + 5))
        with self.store._lock:
            rows = self.store._cx.execute(
                "SELECT auteur, ts, texte, snr, sauts FROM messages WHERE sens='recu' AND canal=? AND ts>=? AND ts<=? ORDER BY ts",
                (t["canal"], debut, fin)).fetchall()
        reponses = [dict(r) for r in rows]
        relayeurs = set(ech.get("tousNoms") or [])
        rep = []
        for n in self.store.list_nodes():
            if n.get("proprio") == "externe" or n.get("statut") in ("prevu", "supprime"):
                continue
            rep.append({"nom": n.get("nom"), "statut": n.get("statut"), "batterie": n.get("batterie"),
                        "maj": n.get("maj"), "relaye": n.get("nom") in relayeurs, "eteint": bool(n.get("eteint"))})
        ok = sum(1 for r in rep if r["statut"] == "en-ligne" and not r["eteint"])
        return {"echos": ech, "reponses": reponses, "repeteurs": rep,
                "synthese": {"relaisDirects": ech.get("directs", 0), "relaisTotal": ech.get("relais", 0),
                             "reponses": len(reponses), "repeteursOk": ok, "repeteursTotal": len(rep)}}

    # ---------- déroulement ----------
    def lancer(self, auto: bool = False) -> dict:
        if self.courant:
            return {"ok": False, "erreur": "Un test est déjà en cours."}
        if self.poller.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        ch = self.api._canal_adrasec()
        if ch is None:
            return {"ok": False, "erreur": "Le canal ADRASEC 06 n'est pas configuré sur le nœud companion."}
        r = self.poller.envoyer_message(TEXTE, canal=ch["index"])
        if not r.get("ok"):
            return r
        op = self.store.get_config("operateur", "") or ""
        with self.store._lock:
            _table(self.store)
            c = self.store._cx.execute(
                "INSERT INTO tests_resilience(debut, texte, canal, canal_nom, auto, operateur) VALUES(?,?,?,?,?,?)",
                (time.time() - 1, TEXTE, ch["index"], ch["nom"], 1 if auto else 0, op))
            self.store._cx.commit()
            self.courant = c.lastrowid
        self.store.add_journal("", op or "PC", f"Test mensuel de résilience lancé{' (automatique)' if auto else ''} "
                               f"sur #{ch['nom'].lstrip('#')} : « {TEXTE} ».", nature="evenement")
        try:
            self.poller.poll_now()  # état des répéteurs pendant le test
        except Exception:  # noqa: BLE001
            pass
        self._fin_anticipee.clear()
        threading.Thread(target=self._attendre_fin, args=(self.courant,), daemon=True).start()
        self.store._bump()
        return {"ok": True, "id": self.courant}

    def terminer(self) -> dict:
        if not self.courant:
            return {"ok": False, "erreur": "Aucun test en cours."}
        self._fin_anticipee.set()
        return {"ok": True}

    def _attendre_fin(self, tid: int):
        fin = time.time() + DUREE
        while time.time() < fin and not self._fin_anticipee.is_set():
            self._fin_anticipee.wait(5)
            self.store._bump()  # rafraîchit le compte à rebours et les résultats partiels
        try:
            self._finaliser(tid)
        except Exception:  # noqa: BLE001
            log.exception("fin du test de résilience")
        finally:
            self.courant = None
            self.store._bump()

    def _finaliser(self, tid: int):
        t = self._get(tid)
        t["fin"] = time.time()
        res = self.resultats(t)
        res["date"] = now_iso()
        pdf = ""
        try:
            from . import rapports
            dossier = self.api._taches.dossier_rapports() if self.api._taches else None
            if dossier:
                pdf = str(dossier / f"Test-resilience-ADRASEC06-{datetime.now().strftime('%Y-%m-%d-%Hh%M')}.pdf")
                rapports.pdf_test_resilience(self.store, {**t, "resultats": res}, pdf)
        except Exception:  # noqa: BLE001
            log.exception("PDF du test de résilience")
            pdf = ""
        with self.store._lock:
            self.store._cx.execute("UPDATE tests_resilience SET fin=?, resultats=?, pdf=? WHERE id=?",
                                   (t["fin"], json.dumps(res, ensure_ascii=False, default=str), pdf, tid))
            self.store._cx.commit()
        s = res["synthese"]
        texte = (f"Test mensuel de résilience terminé : relayé par {s['relaisDirects']} répéteur(s) "
                 f"({s['relaisTotal']} relais au total), {s['reponses']} accusé(s) reçu(s), "
                 f"répéteurs ADRASEC en ligne {s['repeteursOk']}/{s['repeteursTotal']}.")
        self.store.add_journal("", "AUTO", texte, auto=True)
        self.alerte("info", "", texte)
        if pdf and self.store.get_config("rapport_mail", False):
            try:
                from . import courriel
                courriel.envoyer(self.store, f"[ADRASEC 06] Test mensuel de résilience — {datetime.now().strftime('%d/%m/%Y')}",
                                 texte + "\n\nLe rapport détaillé est joint.\n\n— Message automatique du logiciel de supervision.",
                                 [pdf])
            except Exception:  # noqa: BLE001
                log.exception("e-mail du test de résilience")

    def _planificateur(self):
        while not self._stop.wait(60):
            try:
                r = self.reglages()
                if not r["auto"] or self.courant:
                    continue
                now = datetime.now()
                cle = now.strftime("%Y-%m")
                if now.day != r["jour"] or self.store.get_config("test_mois", "") == cle:
                    continue
                try:
                    hh, mm = [int(x) for x in r["heure"].replace("h", ":").split(":")[:2]]
                except ValueError:
                    hh, mm = 20, 0
                if (now.hour, now.minute) < (hh, mm):
                    continue
                res = self.lancer(auto=True)
                if res.get("ok"):
                    self.store.set_config("test_mois", cle)
                else:
                    log.warning("Test de résilience automatique : %s", res.get("erreur"))
            except Exception:  # noqa: BLE001
                log.exception("planificateur du test de résilience")
