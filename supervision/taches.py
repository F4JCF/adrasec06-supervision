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
from pathlib import Path

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
            "url": exe.get("browser_download_url") if exe else None, "taille": exe.get("size") if exe else None,
            "notes": (data.get("body") or "")[:2000], "page": data.get("html_url")}


COOKIE_PYI = b"MEI\x0c\x0b\x0a\x0b\x0e"  # signature de fin d'un exécutable PyInstaller complet


def _exe_complet(chemin: Path, taille_attendue=None) -> bool:
    try:
        n = chemin.stat().st_size
        if n < 1_000_000 or (taille_attendue and n != int(taille_attendue)):
            return False
        with open(chemin, "rb") as f:
            f.seek(max(0, n - 8192))
            return COOKIE_PYI in f.read()
    except OSError:
        return False


def installer_maj(url: str, quitter, taille=None) -> dict:
    """Télécharge le nouvel exécutable, remplace l'actuel après fermeture, puis relance."""
    if not getattr(sys, "frozen", False) or not sys.platform.startswith("win"):
        return {"ok": False, "erreur": "La mise à jour automatique ne fonctionne que dans l'exécutable Windows."}
    exe = sys.executable
    dossier = data_dir() / "mise-a-jour"
    dossier.mkdir(exist_ok=True)
    neuf = dossier / "Supervision-ADRASEC06-nouveau.exe"
    erreur = None
    for _essai in range(3):  # un téléchargement coupé est recommencé
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Supervision-ADRASEC06"})
            with urllib.request.urlopen(req, timeout=60) as r, open(neuf, "wb") as f:
                annonce = r.headers.get("Content-Length")
                while True:
                    bloc = r.read(1 << 16)
                    if not bloc:
                        break
                    f.write(bloc)
            attendu = taille or (int(annonce) if annonce and annonce.isdigit() else None)
            if _exe_complet(neuf, attendu):
                erreur = None
                break
            erreur = "fichier incomplet"
        except Exception as e:  # noqa: BLE001
            erreur = str(e)
    if erreur:
        try:
            neuf.unlink()
        except OSError:
            pass
        return {"ok": False, "erreur": f"Téléchargement incomplet ({erreur}). Rien n'a été modifié ; réessayez plus tard."}
    # l'exécutable PyInstaller « un seul fichier » tourne en deux processus : on attend les deux
    pids = {os.getpid(), os.getppid()}
    # attente de la fermeture (30 s maximum), puis arrêt forcé si l'ancienne version traîne encore
    attente = "".join(
        f'set t=0\r\n:attente{i}\r\n'
        f'tasklist /FI "PID eq {p}" 2>nul | find "{p}" >nul || goto fini{i}\r\n'
        f'set /a t+=1\r\n'
        f'if %t% geq 30 (taskkill /F /PID {p} >nul 2>&1 & goto fini{i})\r\n'
        f'ping -n 2 127.0.0.1 >nul\r\ngoto attente{i}\r\n:fini{i}\r\n'
        for i, p in enumerate(sorted(pids)))
    journal = dossier / "installation.log"
    ancien = dossier / "Supervision-ADRASEC06-precedent.exe"
    temp = exe + ".maj"
    bat = dossier / "installer.bat"
    bat.write_text(
        "@echo off\r\n"
        f'echo %date% %time% attente fermeture >> "{journal}"\r\n'
        + attente +
        "ping -n 3 127.0.0.1 >nul\r\n"
        "set n=0\r\n"
        ":copie\r\n"
        f'copy /Y "{exe}" "{ancien}" >nul 2>&1\r\n'
        f'copy /Y "{neuf}" "{temp}" >nul 2>&1 || goto echec\r\n'
        f'move /Y "{temp}" "{exe}" >nul 2>&1 && (echo %date% %time% copie OK >> "{journal}" & del "{neuf}" & goto lance)\r\n'
        ":echec\r\n"
        "set /a n+=1\r\n"
        f'if %n% geq 30 (echo %date% %time% copie impossible >> "{journal}" & goto lance)\r\n'
        "ping -n 2 127.0.0.1 >nul\r\n"
        "goto copie\r\n"
        ":lance\r\n"
        f'start "" "{exe}"\r\n'
        f'echo %date% %time% relance >> "{journal}"\r\n'
        'del "%~f0"\r\n', encoding="mbcs" if sys.platform.startswith("win") else "utf-8")
    # console invisible, partagée par toutes les commandes du script (aucune fenêtre ne s'ouvre)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    si = None
    if hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
    # environnement propre : sinon le nouvel exécutable croit être le « fils » de l'ancien et cherche
    # ses fichiers temporaires (_MEI…) déjà effacés → « Failed to load Python DLL »
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("_PYI_", "_MEI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=flags, startupinfo=si, close_fds=True, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            try:
                self.rapport_si_besoin()
            except Exception as e:  # noqa: BLE001
                log.warning("Rapport hebdomadaire : %s", e)
            try:
                self.externe_si_besoin()
            except Exception as e:  # noqa: BLE001
                log.warning("Données extérieures : %s", e)
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

    def externe_si_besoin(self):
        """Nettoyage des régions, firmware MeshCore (12 h), analyseur public (24 h, si activé)."""
        from . import externe
        externe.nettoyer_regions(self.store)
        now = time.time()
        if now - float(self.store.get_config("fw_verif_ts", 0) or 0) > 12 * 3600:
            self.store.set_config("fw_verif_ts", now)
            externe.verifier_firmware(self.store, self.alerte)
            self.store._bump()
        if self.store.get_config("vue_externe_auto", True) and now - float(self.store.get_config("vue_externe_ts", 0) or 0) > 24 * 3600:
            self.store.set_config("vue_externe_ts", now)
            r = externe.vue_externe(self.store)
            if r.get("ok"):
                externe.verifier_fuites(self.store, self.alerte)
            self.store._bump()

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

    # ----- rapport hebdomadaire -----
    def reglages_rapport(self) -> dict:
        return {"actif": bool(self.store.get_config("rapport_auto", False)),
                "jour": int(self.store.get_config("rapport_jour", 0) or 0),
                "heure": self.store.get_config("rapport_heure", "08:00") or "08:00",
                "dossier": self.store.get_config("rapport_dossier", "") or "",
                "mail": bool(self.store.get_config("rapport_mail", False)),
                "dernier": self.store.get_config("rapport_dernier_fichier", "") or ""}

    def dossier_rapports(self) -> Path:
        d = self.store.get_config("rapport_dossier", "") or ""
        if not d:
            b = self.store.get_config("dossier_sauvegarde", "") or ""
            d = str(Path(b) / "Rapports") if b else str(data_dir() / "rapports")
        Path(d).mkdir(parents=True, exist_ok=True)
        return Path(d)

    def generer_rapport(self, envoyer_mail: bool | None = None) -> dict:
        from . import courriel, rapports
        chemin = self.dossier_rapports() / f"Rapport-reseau-ADRASEC06-{datetime.now().strftime('%Y-%m-%d')}.pdf"
        rapports.pdf_etat(self.store, str(chemin))
        self.store.set_config("rapport_dernier_fichier", str(chemin))
        res = {"ok": True, "chemin": str(chemin)}
        if envoyer_mail if envoyer_mail is not None else self.store.get_config("rapport_mail", False):
            m = courriel.envoyer(self.store, f"[ADRASEC 06] Rapport hebdomadaire du réseau MeshCore — {datetime.now().strftime('%d/%m/%Y')}",
                                 "Ci-joint le rapport hebdomadaire de l'état du réseau MeshCore ADRASEC 06 "
                                 "(disponibilité, batteries, tendances, alertes).\n\n— Message automatique du logiciel de supervision.",
                                 [str(chemin)])
            res["mail"] = m
        self.store._bump()
        return res

    def rapport_si_besoin(self):
        r = self.reglages_rapport()
        if not r["actif"]:
            return
        maintenant = datetime.now()
        semaine = maintenant.strftime("%G-S%V")
        if self.store.get_config("rapport_semaine", "") == semaine or maintenant.weekday() != r["jour"]:
            return
        try:
            hh, mm = [int(x) for x in r["heure"].replace("h", ":").split(":")[:2]]
        except ValueError:
            hh, mm = 8, 0
        if (maintenant.hour, maintenant.minute) < (hh, mm):
            return
        self.store.set_config("rapport_semaine", semaine)
        res = self.generer_rapport()
        log.info("Rapport hebdomadaire : %s", res)
        self.alerte("info", "", f"Rapport hebdomadaire enregistré : {res['chemin']}"
                    + ("" if not res.get("mail") else (" (envoyé par e-mail)" if res["mail"].get("ok") else " (e-mail non envoyé)")))

    def rappels_maintenance(self):
        limite = date.today() + timedelta(days=7)
        for n in self.store.list_nodes():
            pv = n.get("prochaineVisite")
            if not pv or n.get("proprio") == "externe" or n.get("statut") == "supprime":
                continue
            try:
                d = date.fromisoformat(str(pv)[:10])
            except ValueError:
                continue
            if d <= limite and n.get("rappelVisite") != pv:
                quand = "dépassée" if d < date.today() else ("aujourd'hui" if d == date.today() else f"le {d.strftime('%d/%m/%Y')}")
                self.alerte("attention", n.get("nom", ""), f"Visite de maintenance à prévoir sur {n.get('nom')} : {quand}.")
                self.store.patch_node(n["id"], {"rappelVisite": pv})
