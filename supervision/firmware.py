"""Mise à jour du firmware du nœud companion (Wio Tracker L1 / L1 Pro, nRF52, chargeur UF2).

1. Version installée (lue sur le nœud) et dernière version « companion » publiée par MeshCore sur GitHub.
2. Sauvegarde des contacts et des canaux (avec leurs clés) avant la mise à jour.
3. Téléchargement du fichier .uf2 « Companion Bluetooth » de la carte.
4. Le nœud est mis en mode mise à jour (double appui sur RST) : il apparaît comme un disque « TRACKER L1 »
   (fichier INFO_UF2.TXT) ; le logiciel y copie le fichier .uf2, ce qui reprogramme le nœud.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import string
import sys
import time
import urllib.request
from pathlib import Path

from .store import data_dir

log = logging.getLogger("supervision")

API = "https://api.github.com/repos/meshcore-dev/MeshCore/releases?per_page=40"
FLASHER = "https://flasher.meshcore.co.uk/"


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Supervision-ADRASEC06", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _version(txt: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", txt or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def _score(nom: str, modele: str) -> int:
    """Pertinence d'un fichier pour un companion Bluetooth sur Wio Tracker L1 (Pro)."""
    n = nom.lower()
    if not n.endswith(".uf2") or any(x in n for x in ("erase", "bootloader", "repeater", "room", "sensor")):
        return -1
    if not re.search(r"tracker[_\-]?l1|wio[_\-]?l1|l1[_\-]?pro", n):
        return -1
    s = 10
    if "ble" in n or "bluetooth" in n:
        s += 5
    if "usb" in n or "wifi" in n:
        s -= 5
    if "companion" in n:
        s += 2
    m = (modele or "").lower()
    for variante in ("eink", "e-ink", "1w"):
        if (variante in n) != (variante.replace("-", "") in m.replace("-", "")):
            s -= 8
    return s


def derniere_version(modele: str = "") -> dict:
    try:
        rel = json.loads(_get(API).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erreur": f"Impossible de consulter les versions MeshCore (Internet) : {e}"}
    for r in rel:
        tag = r.get("tag_name") or ""
        if not tag.lower().startswith("companion") or r.get("prerelease") or r.get("draft"):
            continue
        fichiers = sorted(((_score(a["name"], modele), a["name"], a["browser_download_url"], a.get("size") or 0)
                           for a in r.get("assets") or []), reverse=True)
        fichiers = [f for f in fichiers if f[0] >= 0]
        return {"ok": True, "tag": tag, "version": "v" + ".".join(str(x) for x in _version(tag)),
                "date": (r.get("published_at") or "")[:10], "notes": (r.get("body") or "")[:1500],
                "fichiers": [{"nom": f[1], "url": f[2], "taille": f[3], "conseille": i == 0} for i, f in enumerate(fichiers)],
                "page": r.get("html_url") or ""}
    return {"ok": False, "erreur": "Aucune version « companion » trouvée sur GitHub."}


def dossier() -> Path:
    d = data_dir() / "firmware"
    d.mkdir(parents=True, exist_ok=True)
    return d


def telecharger(url: str, nom: str) -> dict:
    if not url.startswith("https://github.com/meshcore-dev/MeshCore/releases/download/"):
        return {"ok": False, "erreur": "Adresse de téléchargement inattendue."}
    nom = re.sub(r"[^\w.\-]", "_", nom)
    cible = dossier() / nom
    try:
        data = _get(url, timeout=120)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erreur": f"Téléchargement impossible : {e}"}
    if len(data) < 50000 or data[:4] != b"UF2\n":
        return {"ok": False, "erreur": "Le fichier téléchargé n'est pas un firmware UF2 valide."}
    cible.write_bytes(data)
    return {"ok": True, "chemin": str(cible), "taille": len(data)}


def sauvegarder(contacts: dict, canaux: list, secrets: dict, infos: dict) -> dict:
    d = dossier()
    f = d / f"sauvegarde-companion-{time.strftime('%Y-%m-%d-%Hh%M')}.json"
    doc = {"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "noeud": infos,
           "canaux": [{"index": c["index"], "nom": c["nom"], "cle": secrets.get(c["index"], "")} for c in canaux],
           "contacts": [{"nom": c.get("adv_name"), "cle": c.get("public_key"), "type": c.get("type"),
                         "lat": c.get("adv_lat"), "lon": c.get("adv_lon")} for c in contacts.values()]}
    f.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "chemin": str(f), "contacts": len(doc["contacts"]), "canaux": len(doc["canaux"])}


def disques_uf2() -> list[str]:
    """Disques en mode mise à jour UF2 (fichier INFO_UF2.TXT à la racine)."""
    out = []
    if sys.platform.startswith("win"):
        racines = [f"{l}:\\" for l in string.ascii_uppercase if l not in "AB"]
    else:
        racines = [str(p) for base in ("/media", "/run/media", "/Volumes") if os.path.isdir(base)
                   for p in Path(base).rglob("*") if p.is_dir() and len(p.parts) <= 5]
    for r in racines:
        try:
            if os.path.isfile(os.path.join(r, "INFO_UF2.TXT")):
                out.append(r)
        except OSError:
            pass
    return out


def flasher(chemin: str, attente: int = 90, arret=None) -> dict:
    """Attend le disque UF2 du nœud (double appui sur RST) puis y copie le firmware."""
    src = Path(chemin)
    if not src.is_file():
        return {"ok": False, "erreur": "Téléchargez d'abord le firmware."}
    fin = time.time() + attente
    disque = None
    while time.time() < fin:
        if arret is not None and arret.is_set():
            return {"ok": False, "erreur": "Mise à jour annulée."}
        d = disques_uf2()
        if d:
            disque = d[0]
            break
        time.sleep(1)
    if not disque:
        return {"ok": False, "erreur": "Le nœud n'est pas apparu en mode mise à jour (disque « TRACKER L1 »). "
                                       "Appuyez deux fois rapidement sur le bouton RST, puis réessayez."}
    try:
        info = Path(disque, "INFO_UF2.TXT").read_text(errors="ignore")
    except OSError:
        info = ""
    try:
        time.sleep(1.5)  # laisse Windows finir de monter le disque
        shutil.copyfile(src, Path(disque) / src.name)
    except OSError as e:
        # le nœud redémarre souvent avant la fin de la copie, ce qui est normal
        if Path(disque).exists():
            return {"ok": False, "erreur": f"Copie impossible sur {disque} : {e}"}
    log.info("Firmware %s copié sur %s (%s)", src.name, disque, info.splitlines()[:2])
    return {"ok": True, "disque": disque, "info": info.strip()[:300]}
