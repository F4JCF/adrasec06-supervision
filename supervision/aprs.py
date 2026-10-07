"""Réception APRS par Internet (APRS-IS), en lecture seule.

Le logiciel se connecte à un serveur APRS-IS avec le code d'accès -1 (réception uniquement, aucune émission)
et un filtre : indicatifs suivis (b/…) et/ou rayon autour d'un point (r/lat/lon/km).
Les positions reçues sont enregistrées et affichées sur la carte.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time

from .store import Store, now_iso

log = logging.getLogger("supervision")

try:
    import aprslib
except Exception:  # pragma: no cover
    aprslib = None

SERVEUR = ("rotate.aprs2.net", 14580)
CENTRE = (43.60, 7.00)  # Mougins


def base_call(call: str) -> str:
    """F1ABC-9 → F1ABC"""
    return (call or "").upper().split("-")[0].strip()


def indicatifs(texte) -> list[str]:
    if isinstance(texte, list):
        texte = " ".join(texte)
    out = []
    for t in re.split(r"[\s,;]+", str(texte or "").upper()):
        t = re.sub(r"[^A-Z0-9\-*]", "", t)
        if t and t not in out:
            out.append(t)
    return out[:200]


def construire_filtre(calls: list[str], rayon_km: float, centre=CENTRE) -> str:
    parts = []
    if calls:
        # un indicatif sans SSID suit aussi toutes ses variantes (F1ABC → F1ABC*)
        parts.append("b/" + "/".join(c if ("-" in c or c.endswith("*")) else c + "*" for c in calls))
    if rayon_km and rayon_km > 0:
        parts.append(f"r/{centre[0]:.3f}/{centre[1]:.3f}/{int(rayon_km)}")
    return " ".join(parts)


class AprsIS:
    def __init__(self, store: Store, version: str):
        self.store = store
        self.version = version
        self.etat = {"actif": False, "connecte": False, "serveur": "", "message": "APRS désactivé",
                     "paquets": 0, "dernier": None}
        self._stop = threading.Event()
        self._thread = None
        self._sock = None
        self.redemarrer()

    # ---------- réglages ----------
    def reglages(self) -> dict:
        return {"actif": bool(self.store.get_config("aprs_actif", False)),
                "indicatifs": self.store.get_config("aprs_indicatifs", []) or [],
                "rayon": float(self.store.get_config("aprs_rayon", 0) or 0)}

    def regler(self, actif=None, liste=None, rayon=None) -> dict:
        if actif is not None:
            self.store.set_config("aprs_actif", bool(actif))
        if liste is not None:
            self.store.set_config("aprs_indicatifs", indicatifs(liste))
        if rayon is not None:
            try:
                self.store.set_config("aprs_rayon", max(0, min(500, float(rayon))))
            except (TypeError, ValueError):
                pass
        self.redemarrer()
        return {"ok": True, **self.reglages()}

    def redemarrer(self):
        self.arreter()
        r = self.reglages()
        self.etat.update(actif=r["actif"], connecte=False)
        if not r["actif"]:
            self.etat["message"] = "APRS désactivé"
            self.store._bump()
            return
        if aprslib is None:
            self.etat["message"] = "Décodeur APRS absent de cette version."
            return
        if not construire_filtre(r["indicatifs"], r["rayon"]):
            self.etat["message"] = "Indiquez des indicatifs à suivre ou un rayon."
            self.store._bump()
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._boucle, args=(self._stop,), name="aprs-is", daemon=True)
        self._thread.start()

    def arreter(self):
        self._stop.set()
        s = self._sock
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        self._sock = None

    # ---------- connexion ----------
    def _boucle(self, stop: threading.Event):
        attente = 5
        while not stop.is_set():
            r = self.reglages()
            filtre = construire_filtre(r["indicatifs"], r["rayon"])
            login = base_call(self.store.get_config("operateur", "") or "") or "NOCALL"
            try:
                self.etat["message"] = f"Connexion à {SERVEUR[0]}…"
                self.store._bump()
                s = socket.create_connection(SERVEUR, timeout=20)
                self._sock = s
                s.settimeout(90)
                s.sendall(f"user {login} pass -1 vers ADRASEC06-Supervision {self.version} filter {filtre}\r\n".encode())
                self.etat.update(connecte=True, serveur=SERVEUR[0],
                                 message=f"Connecté à APRS-IS ({SERVEUR[0]}) · filtre {filtre}")
                self.store._bump()
                log.info("APRS-IS connecté, filtre %s", filtre)
                attente = 5
                buf = b""
                dernier_keepalive = time.time()
                while not stop.is_set():
                    try:
                        data = s.recv(4096)
                    except socket.timeout:
                        raise ConnectionError("pas de données depuis 90 s")
                    if not data:
                        raise ConnectionError("connexion fermée par le serveur")
                    buf += data
                    while b"\n" in buf:
                        ligne, buf = buf.split(b"\n", 1)
                        self._traiter(ligne.decode("latin-1", "ignore").strip())
                    if time.time() - dernier_keepalive > 600:
                        s.sendall(b"# keepalive ADRASEC06-Supervision\r\n")
                        dernier_keepalive = time.time()
            except Exception as e:  # noqa: BLE001
                if stop.is_set():
                    break
                self.etat.update(connecte=False, message=f"APRS-IS indisponible ({e}) — nouvel essai dans {attente} s")
                self.store._bump()
                log.warning("APRS-IS : %s", e)
                stop.wait(attente)
                attente = min(attente * 2, 300)
            finally:
                try:
                    if self._sock is not None:
                        self._sock.close()
                except OSError:
                    pass
                self._sock = None
        self.etat["connecte"] = False

    def _traiter(self, ligne: str):
        if not ligne or ligne.startswith("#"):
            return
        try:
            p = aprslib.parse(ligne)
        except Exception:
            return
        lat, lon = p.get("latitude"), p.get("longitude")
        if lat is None or lon is None:
            return
        self.store.add_aprs({
            "call": p.get("from", ""), "lat": float(lat), "lon": float(lon),
            "vitesse": p.get("speed"), "cap": p.get("course"), "altitude": p.get("altitude"),
            "commentaire": (p.get("comment") or "")[:120], "symbole": (p.get("symbol_table") or "") + (p.get("symbol") or ""),
        })
        self.etat["paquets"] += 1
        self.etat["dernier"] = now_iso()
