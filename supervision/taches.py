"""Tâches de fond : sauvegarde quotidienne, rappels de maintenance, mises à jour du logiciel."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta

from .store import Store, data_dir

log = logging.getLogger("supervision")
DEPOT = "F4JCF/adrasec06-supervision"
API_RELEASE = f"https://api.github.com/repos/{DEPOT}/releases/latest"


# ---------- versions ----------
def _v(s: str) -> tuple:
    out = []
    for part in str(s or "").lstrip("vV").split("."):
        try:
            out.append(int("".join(ch for ch in part if ch.isdigit()) or 0))
        except ValueError:
            out.append(0)
    return tuple((out + [0, 0, 0])[:3])


def verifier_maj(version_actuelle: str) -> dict:
    """Interroge GitHub : y a-t-il une version plus récente que celle-ci ?"""
    try:
        req = urllib.request.Request(API_RELEASE, headers={"User-Agent": "Supervision-ADRASEC06",
                                                           "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erreur": "Vérification impossible (pas d'Internet, ou dépôt GitHub privé).", "detail": str(e)}
    tag = data.get("tag_name", "")
    exe = next((a for a in data.get("assets", []) if a.get("name", "").lower().endswith(".exe")), None)
    return {"ok": True, "derniere": tag.lstrip("vV"), "actuelle": version_actuelle,
            "disponible": bool(exe) and _v(tag) > _v(version_actuelle),
            "url": exe.get("browser_download_url") if exe else None,
            "notes": (data.get("body") or "")[:2000], "page": data.get("html_url")}


def installer_maj(url: str, quitter) -> dict:
    """Télécharge le nouvel exécutable, remplace l'actuel après fermeture, puis relance."""
    if not getattr(sys, "frozen", False) or not sys.platform.startswith("win"):
        return {"ok": False, "erreur": "La mise à jour automatique ne fonctionne que dans l'exécutable Windows."}
    exe = sys.executable
    dossier = data_dir() / "mise-a-jour"
    dossier.mkdir(exist_ok=True)
    neuf = dossier / "Supervision-ADRASEC06-nouveau.exe"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Supervision-ADRASEC06"})
        with urllib.request.urlopen(req, timeout=180) as r, open(neuf, "wb") as f:
            while True:
                bloc = r.read(1 << 16)
                if not bloc:
                    break
                f.write(bloc)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erreur": f"Téléchargement impossible : {e}"}
    if neuf.stat().st_size < 1_000_000:
        return {"ok": False, "erreur": "Le fichier téléchargé est incomplet. Réessayez plus tard."}
    pid = os.getpid()
    bat = dossier / "installer.bat"
    bat.write_text(
        "@echo off\r\n"
        ":attente\r\n"
        f'tasklist /FI "PID eq {pid}" | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto attente)\r\n'
        "timeout /t 2 /nobreak >nul\r\n"
        f'copy /Y "{neuf}" "{exe}" >nul\r\n'
        f'start "" "{exe}"\r\n'
        'del "%~f0"\r\n', encoding="mbcs" if sys.platform.startswith("win") else "utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=flags, close_fds=True)
    threading.Timer(1.0, quitter).start()
    return {"ok": True}


# ---------- tâches périodiques ----------
class Taches:
    def __init__(self, store: Store, alerte, version: str):
        self.store = store
        self.alerte = alerte
        self.version = version
        self.maj = None
        self._t = threading.Thread(target=self._boucle, name="taches", daemon=True)
        self._t.start()

    def _boucle(self):
        time.sleep(20)
        derniere_verif = 0.0
        while True:
            try:
                self.sauvegarde_si_besoin()
            except Exception as e:  # noqa: BLE001
                log.warning("Sauvegarde automatique : %s", e)
            try:
                self.rappels_maintenance()
            except Exception as e:  # noqa: BLE001
                log.warning("Rappels de maintenance : %s", e)
            if self.store.get_config("maj_auto", True) and time.time() - derniere_verif > 6 * 3600:
                derniere_verif = time.time()
                r = verifier_maj(self.version)
                if r.get("ok"):
                    self.maj = r
                    if r.get("disponible") and self.store.get_config("maj_annoncee", "") != r["derniere"]:
                        self.store.set_config("maj_annoncee", r["derniere"])
                        self.alerte("info", "", f"Nouvelle version {r['derniere']} disponible (Réglages → Mettre à jour).")
                    self.store._bump()
            time.sleep(600)

    def sauvegarde_si_besoin(self) -> str | None:
        dossier = self.store.get_config("dossier_sauvegarde", "")
        if not dossier:
            return None
        der = self.store.get_config("derniere_sauvegarde", "")
        if der:
            try:
                if datetime.now().astimezone() - datetime.fromisoformat(der) < timedelta(hours=24):
                    return None
            except ValueError:
                pass
        chemin = self.store.backup(dossier)
        log.info("Sauvegarde automatique : %s", chemin)
        return chemin

    def rappels_maintenance(self):
        limite = date.today() + timedelta(days=7)
        for n in self.store.list_nodes():
            pv = n.get("prochaineVisite")
            if not pv or n.get("proprio") == "externe":
                continue
            try:
                d = date.fromisoformat(str(pv)[:10])
            except ValueError:
                continue
            if d <= limite and n.get("rappelVisite") != pv:
                quand = "dépassée" if d < date.today() else ("aujourd'hui" if d == date.today() else f"le {d.strftime('%d/%m/%Y')}")
                self.alerte("attention", n.get("nom", ""), f"Visite de maintenance à prévoir sur {n.get('nom')} : {quand}.")
                self.store.patch_node(n["id"], {"rappelVisite": pv})
