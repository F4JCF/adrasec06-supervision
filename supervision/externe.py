"""Données publiques extérieures : analyseur MeshCore France (meshstats) et versions du firmware MeshCore.

- « Ce que la France voit de nous » : quels observateurs publics entendent nos répéteurs, et nos messages
  privés vus en inondation hors des Alpes-Maritimes (signe d'un chemin inconnu).
- Dernière version publiée du firmware « repeater » sur GitHub.
Tout est en lecture seule et ne transmet que des clés publiques (déjà publiques sur le réseau).
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request

from .store import Store, now_iso

log = logging.getLogger("supervision")

MESHSTATS = "https://meshstats.serveurperso.in"
GITHUB = "https://api.github.com/repos/meshcore-dev/MeshCore/releases?per_page=60"
LOCAL = {"NCE"}  # code IATA des observateurs des Alpes-Maritimes (Nice)


def _get_json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": "Supervision-ADRASEC06", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _version(txt: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", txt or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


# ---------- analyseur public ----------
def vue_externe(store: Store) -> dict:
    """Interroge l'analyseur public pour chaque répéteur ADRASEC dont la clé complète est connue."""
    reps = [n for n in store.list_nodes() if n.get("proprio") == "adrasec" and n.get("statut") not in ("prevu", "supprime")
            and len(str(n.get("cleComplete") or "")) == 64]
    if not reps:
        return {"ok": False, "erreur": "Aucun répéteur ADRASEC avec une clé publique complète (lue à la première interrogation)."}
    cle_comp = (store.get_config("cle_companion", "") or "").lower()
    src = cle_comp[:2]
    out_reps, fuites, vus = [], [], set()
    for n in reps:
        pk = n["cleComplete"].lower()
        try:
            info = _get_json(f"{MESHSTATS}/api/nodes/{pk}")
            pq = _get_json(f"{MESHSTATS}/api/packets?node={pk}&limit=500")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Analyseur public injoignable ({e})."}
        nd = (info or {}).get("node") or {}
        obs = {}
        for p in (pq or {}).get("packets", []):
            o = p.get("observer_name") or "?"
            x = obs.setdefault(o, {"nom": o, "iata": p.get("observer_iata") or "", "paquets": 0, "propres": 0, "dernier": ""})
            x["paquets"] += 1
            x["dernier"] = max(x["dernier"], p.get("timestamp") or "")
            try:
                d = json.loads(p.get("decoded_json") or "{}")
            except ValueError:
                d = {}
            if (d.get("pubKey") or "").lower() == pk:
                x["propres"] += 1
            # message privé de notre companion, en inondation, vu par un observateur hors du 06
            if src and d.get("type") == "TXT_MSG" and (d.get("srcHash") or "").lower() == src and p.get("route_type") == 0 \
                    and (p.get("observer_iata") or "") not in LOCAL and p.get("hash") not in vus:
                vus.add(p.get("hash"))
                fuites.append({"date": p.get("timestamp"), "observateur": o, "iata": p.get("observer_iata"),
                               "dest": d.get("destHash"), "via": n.get("nom")})
        out_reps.append({"nom": n.get("nom"), "connu": bool(nd), "premier": nd.get("first_seen"), "dernier": nd.get("last_seen"),
                         "relais24h": nd.get("relay_count_24h"), "note": nd.get("usefulness_grade"),
                         "observateurs": sorted(obs.values(), key=lambda x: -x["paquets"])[:25],
                         "horsDepartement": sum(1 for x in obs.values() if x["iata"] not in LOCAL)})
    fuites.sort(key=lambda f: f["date"] or "", reverse=True)
    res = {"ok": True, "date": now_iso(), "repeteurs": out_reps, "fuites": fuites[:50], "nbFuites": len(fuites),
           "companionConnu": bool(src), "source": MESHSTATS}
    store.set_config("vue_externe", res)
    return res


def verifier_fuites(store: Store, alerte) -> None:
    """Alerte (une fois par message) si un message privé est vu en inondation hors du département."""
    v = store.get_config("vue_externe", {}) or {}
    if not v.get("fuites"):
        return
    der = v["fuites"][0].get("date") or ""
    if der and der > (store.get_config("fuite_alertee", "") or ""):
        store.set_config("fuite_alertee", der)
        alerte("attention", "", f"{v.get('nbFuites')} message(s) privé(s) de votre companion vu(s) en inondation hors des "
                                f"Alpes-Maritimes (analyseur public) : vérifiez les chemins (Messagerie → Chemin).")


# ---------- firmware des répéteurs ----------
def derniere_version_repeteur() -> dict:
    try:
        rel = _get_json(GITHUB)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erreur": f"GitHub injoignable ({e})."}
    cand = [r for r in rel if str(r.get("tag_name", "")).startswith("repeater-v") and not r.get("prerelease") and not r.get("draft")]
    if not cand:
        return {"ok": False, "erreur": "Aucune version « repeater » trouvée."}
    r = max(cand, key=lambda r: _version(r["tag_name"]))
    v = "v" + ".".join(str(x) for x in _version(r["tag_name"]))
    return {"ok": True, "version": v, "date": (r.get("published_at") or "")[:10], "url": r.get("html_url"),
            "notes": (r.get("body") or "")[:1500]}


def verifier_firmware(store: Store, alerte) -> dict:
    r = derniere_version_repeteur()
    if not r.get("ok"):
        return r
    r["verifie"] = now_iso()
    store.set_config("fw_repeteur", r)
    if store.get_config("fw_repeteur_annonce", "") != r["version"]:
        retard = [n.get("nom") for n in store.list_nodes() if n.get("proprio") == "adrasec" and n.get("firmware")
                  and n.get("statut") not in ("prevu", "supprime") and _version(n["firmware"]) < _version(r["version"])]
        store.set_config("fw_repeteur_annonce", r["version"])
        if retard:
            alerte("info", "", f"Firmware MeshCore {r['version']} publié le {r['date']} : à installer sur " + ", ".join(retard) + ".")
    return r


# ---------- nettoyage ----------
def nettoyer_regions(store: Store) -> int:
    """Retire des réglages de régions les répéteurs qui n'existent plus sous ce nom (renommés ou supprimés)."""
    par = store.get_config("regions_repeteurs", {}) or {}
    noms = {n.get("nom") for n in store.list_nodes() if n.get("statut") != "supprime"}
    garde = {k: v for k, v in par.items() if k in noms}
    if len(garde) != len(par):
        store.set_config("regions_repeteurs", garde)
    return len(par) - len(garde)


def purger_noeuds(store: Store, jours: int = 100) -> list[str]:
    """Retire les nœuds voisins (externes) qui n'ont plus été reçus depuis « jours » jours.

    La date retenue est celle de réception par notre nœud companion (vuLe), ou de la dernière interrogation
    réussie (maj) — jamais l'horodatage de l'annonce, qui dépend de l'horloge du nœud distant (souvent fausse).
    Les répéteurs ADRASEC et les sites prévus ne sont jamais retirés. Un nœud retiré revient s'il est de nouveau entendu.
    """
    from datetime import datetime
    def ts(v):
        try:
            return datetime.fromisoformat(str(v)).timestamp()
        except (TypeError, ValueError):
            return 0.0
    limite = time.time() - jours * 86400
    retires = []
    for n in store.list_nodes():
        if n.get("proprio") != "externe" or n.get("statut") in ("prevu",):
            continue
        if not n.get("vuLe") and not n.get("creeLe"):  # nœud d'avant cette version : le délai part d'aujourd'hui
            store.patch_node(n["id"], {"creeLe": now_iso()})
            continue
        dernier = max(ts(n.get("vuLe")), ts(n.get("maj")), ts(n.get("creeLe")))
        if dernier and dernier < limite:
            store.purge_node(n["id"])
            retires.append(n.get("nom") or n["id"])
    if retires:
        store.add_journal("", "AUTO", f"{len(retires)} nœud(s) voisin(s) retiré(s) : plus reçus depuis {jours} jours ("
                          + ", ".join(retires[:20]) + (" …" if len(retires) > 20 else "") + ").", auto=True)
        store._bump()
    return retires
