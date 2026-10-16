"""Supervision MeshCore ADRASEC 06 — programme de bureau (Windows)."""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import webview

import re
import time
from datetime import datetime

from . import aprs, courriel, rapports, resilience, systeme, taches, visibilite, web
from .poller import Poller, list_serial_ports
from .store import Store, data_dir, now_iso, resource_path, slug

VERSION = "1.20.0"

logging.basicConfig(
    filename=str(data_dir() / "supervision.log"), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


class Api:
    """Fonctions appelées par l'interface (window.pywebview.api.*)."""

    def __init__(self, store: Store, poller: Poller):
        self._store = store
        self._poller = poller
        self._window = None
        self._quitter = None
        self._taches = None
        self._aprs = None
        self._courrier = None
        self._web = None
        self._resilience = None

    # ----- lecture -----
    def revision(self):
        return self._store.revision

    def etat(self):
        nodes = self._store.list_nodes()
        for n in nodes:
            n["aMotDePasse"] = self._store.has_password(n["id"])
        return {
            "revision": self._store.revision,
            "noeuds": nodes,
            "journal": self._store.list_journal(),
            "liaison": self._poller.state(),
            "version": VERSION,
            "dossier": str(data_dir()),
            "messages": self._messages_avec_echos(),
            "alertes": self._store.list_alerts(30),
            "exercice": self._store.current_exercise(),
            "mc": ({"entrees": self._store.journal_of_exercise(cur_ex["id"]), "moyens": self._store.list_moyens(cur_ex["id"]),
                    "connus": self._store.moyens_connus()}
                   if (cur_ex := self._store.current_exercise()) else {"entrees": [], "moyens": []}),
            "exercices": self._store.list_exercises()[:30],
            "reglages": {
                "seuilBatterie": float(self._store.get_config("seuil_batterie", 3.5) or 3.5),
                "demarrageAuto": systeme.demarrage_auto_actif(),
                "reduireZone": bool(self._store.get_config("reduire_zone", True)),
                "notifications": bool(self._store.get_config("notifications", True)),
                "fondCarte": self._store.get_config("fond_carte", "osmfr"),
                "dossierSauvegarde": self._store.get_config("dossier_sauvegarde", ""),
                "derniereSauvegarde": self._store.get_config("derniere_sauvegarde", ""),
                "majAuto": bool(self._store.get_config("maj_auto", True)),
                "horlogeAuto": bool(self._store.get_config("horloge_auto", True)),
                "corrBattCompanion": float(self._store.get_config("corr_batt_companion", 0) or 0),
                "mail": courriel.reglages(self._store),
                "mailDernier": self._courrier.dernier if self._courrier else None,
                "web": {**(self._web.reglages() if self._web else {}), "etat": self._web.etat if self._web else {}},
                "rapport": self._taches.reglages_rapport() if self._taches else {},
                "dossierArchives": self._store.get_config("dossier_archives", "") or "",
                "modeles": self.modeles(),
            },
            "maj": self._taches.maj if self._taches else None,
            "traces": self._store.tracks(6),
            "aprs": {"etat": self._aprs.etat if self._aprs else {}, "reglages": self._aprs.reglages() if self._aprs else {},
                     "stations": self._store.aprs_stations(12), "traces": self._store.aprs_traces(6)},
            "couvertures": self._store.list_coverage(),
            "appel": self._store.appel(),
            "resilience": self._resilience.etat() if self._resilience else None,
            "dispo": {n["id"]: {"j7": self._store.availability(n["id"], 7)["pct"],
                                "j30": self._store.availability(n["id"], 30)["pct"]}
                      for n in nodes if n.get("proprio") != "externe" and n.get("statut") not in ("prevu", "supprime")},
        }

    # ----- tableau de bord web (lecture seule) -----
    def _canal_adrasec(self):
        norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
        ch = self._poller.canaux or []
        return next((c for c in ch if norm(c["nom"]) == "adrasec06"), None) or \
            next((c for c in ch if "adrasec06" in norm(c["nom"])), None)

    def tableau_donnees(self):
        nodes = [n for n in self._store.list_nodes() if n.get("statut") != "supprime"]
        garder = ("id", "nom", "statut", "proprio", "lat", "lon", "batterie", "bruit", "firmware", "maj", "eteint", "telemetrie",
                  "horloge", "horlogeEcart", "horlogeLue", "tendances", "autonomie", "voisins", "role")
        norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
        out = []
        for n in nodes:
            d = {k: n.get(k) for k in garder}
            d["encart"] = (n.get("proprio") != "externe" and n.get("statut") not in ("prevu",)) or norm(n.get("nom")) == "fr06mouginsf4jcf"
            if d.get("autonomie"):
                d["autonomie"] = {"jours": d["autonomie"].get("jours")}
            if n.get("proprio") == "externe":
                d["voisins"] = []
            out.append(d)
        ch = self._canal_adrasec()
        msgs = [m for m in self._store.list_messages(400) if m.get("canal") is not None and
                (m["canal"] == ch["index"] if ch else "adrasec06" in norm(m.get("canal_nom")))][-20:][::-1]
        li = self._poller.state()
        return {"version": VERSION, "noeuds": out, "alertes": self._store.list_alerts(12),
                "messages": [{k: m.get(k) for k in ("date", "sens", "auteur", "texte")} for m in msgs],
                "canal": ch["nom"] if ch else "", "appel": self._store.appel(), "exercice": self._store.current_exercise(),
                "liaison": {k: li.get(k) for k in ("connecte", "noeudLocal", "batterie", "position")},
                "equipes": li.get("equipes") or [],
                "dispo": {n["id"]: {"j7": self._store.availability(n["id"], 7)["pct"]} for n in out if n["encart"]}}

    def regler_web(self, actif=None, port=None, code=None):
        return self._web.regler(actif, port, code) if self._web else {"ok": False, "erreur": "Indisponible."}

    # ----- e-mail -----
    def regler_mail(self, options):
        return courriel.regler(self._store, options or {})

    def tester_mail(self):
        r = courriel.envoyer(self._store, "[ADRASEC 06] Essai d'envoi",
                             "Ceci est un e-mail d'essai envoyé par le logiciel de supervision MeshCore ADRASEC 06.\n"
                             "Si vous le recevez, les alertes par e-mail fonctionnent.")
        if self._courrier:
            self._courrier.dernier = {"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": r["ok"], "erreur": r.get("erreur", "")}
        self._store._bump()
        return r

    # ----- rapport hebdomadaire -----
    def regler_rapport(self, options):
        o = options or {}
        for k, ck in (("actif", "rapport_auto"), ("jour", "rapport_jour"), ("heure", "rapport_heure"),
                      ("dossier", "rapport_dossier"), ("mail", "rapport_mail")):
            if k in o:
                v = o[k]
                if k in ("actif", "mail"):
                    v = bool(v)
                elif k == "jour":
                    try:
                        v = max(0, min(6, int(v)))
                    except (TypeError, ValueError):
                        continue
                else:
                    v = str(v or "").strip()
                self._store.set_config(ck, v)
        self._store._bump()
        return {"ok": True}

    def choisir_dossier_rapport(self):
        if not self._window:
            return {"ok": False}
        r = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not r:
            return {"ok": False}
        self._store.set_config("rapport_dossier", r[0] if isinstance(r, (list, tuple)) else r)
        self._store._bump()
        return {"ok": True}

    def generer_rapport(self):
        try:
            return self._taches.generer_rapport()
        except Exception as e:  # noqa: BLE001
            logging.getLogger("supervision").exception("rapport")
            return {"ok": False, "erreur": str(e)}

    def ouvrir_dossier_rapports(self):
        try:
            os.startfile(str(self._taches.dossier_rapports()))  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": str(e)}

    # ----- appel des stations -----
    def candidats_appel(self):
        """Stations qu'on peut appeler : moyens engagés, stations ayant répondu ou écrit sur le canal, compagnons connus."""
        out, vus = [], set()

        def ajoute(nom, source, coche):
            k = re.sub(r"[^A-Z0-9]", "", (nom or "").upper())
            if not k or k in vus:
                return
            vus.add(k)
            out.append({"nom": nom.strip(), "source": source, "coche": coche})
        ex = self._store.current_exercise()
        if ex:
            for m in self._store.list_moyens(ex["id"]):
                if m.get("indicatif") and not m.get("depart"):
                    ajoute(m["indicatif"], "Moyen engagé", True)
        ch = self._canal_adrasec()
        limite = time.time() - 30 * 86400
        for m in self._store.list_messages(2000):
            if m.get("sens") == "recu" and m.get("auteur") and (m.get("ts") or 0) >= limite and ch and m.get("canal") == ch["index"]:
                ajoute(m["auteur"], "A écrit sur le canal (30 j)", True)
        for c in self._poller.contacts_messagerie():
            ajoute(c["nom"], "Contact du nœud companion", False)
        return {"ok": True, "stations": out, "operateur": self._store.get_config("operateur", "") or ""}

    # ----- test mensuel de résilience -----
    def test_resilience(self):
        return self._resilience.lancer() if self._resilience else {"ok": False, "erreur": "Indisponible."}

    def terminer_test_resilience(self):
        return self._resilience.terminer() if self._resilience else {"ok": False}

    def regler_test_resilience(self, options):
        return self._resilience.regler(options) if self._resilience else {"ok": False}

    def pdf_test_resilience(self, tid):
        t = self._resilience._get(int(tid)) if self._resilience else None
        if not t or not t.get("resultats"):
            return {"ok": False, "erreur": "Test introuvable ou pas encore terminé."}
        p = self._choisir(f"test-resilience-adrasec06-{datetime.fromtimestamp(t['debut']).strftime('%Y-%m-%d')}.pdf", "Document PDF (*.pdf)")
        if not p:
            return {"ok": False, "annule": True}
        try:
            rapports.pdf_test_resilience(self._store, t, p)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("supervision").exception("pdf test")
            return {"ok": False, "erreur": f"PDF impossible : {e}"}
        return {"ok": True, "chemin": p}

    def lancer_appel(self, texte=None, stations=None):
        ch = self._canal_adrasec()
        if ch is None:
            return {"ok": False, "erreur": "Le canal ADRASEC 06 n'est pas configuré sur le nœud companion."}
        op = (self._store.get_config("operateur", "") or "").strip()
        texte = (texte or "").strip() or f"APPEL ADRASEC 06 {time.strftime('%H:%M')} de {op or 'PC'} : répondez SVP avec indicatif et position."
        if len(texte.encode("utf-8")) > 150:
            return {"ok": False, "erreur": "Message d'appel trop long (150 octets maximum)."}
        stations = [str(x).strip() for x in (stations or []) if str(x).strip()]
        r = self._poller.envoyer_message(texte, canal=ch["index"])
        if not r.get("ok"):
            return r
        ex = self._store.current_exercise()
        aid = self._store.start_appel(ch["index"], ch["nom"], texte, ex["id"] if ex else None, op, stations)
        if ex:
            self._store.add_journal("", op or "PC", f"Appel des stations lancé sur #{ch['nom'].lstrip('#')} : « {texte} »"
                                    + (f" — stations appelées : {', '.join(stations)}" if stations else ""),
                                    exercice=ex["id"], emetteur=op or "PC", destinataire="Toutes stations", nature="demande")
        return {"ok": True, "id": aid}

    def clore_appel(self):
        a = self._store.appel()
        if not a or not a["enCours"]:
            return {"ok": False, "erreur": "Aucun appel en cours."}
        self._store.stop_appel(a["id"])
        a = self._store.appel(a["id"])
        rep = a["reponses"]
        hm = lambda ts: time.strftime('%H:%M', time.localtime(ts))
        if a.get("attendus"):
            ok = [f"{x['nom']} ({hm(x['ts'])})" for x in a["attendus"] if x["repondu"]]
            ko = [x["nom"] for x in a["attendus"] if not x["repondu"]]
            autres = [f"{r['auteur']} ({hm(r['ts'])})" for r in a.get("autres") or []]
            texte = (f"Appel des stations de {hm(a['debut'])} clos : {len(ok)}/{len(a['attendus'])} station(s) appelée(s) ont répondu"
                     + (f" : {', '.join(ok)}" if ok else "") + "."
                     + (f" Sans réponse : {', '.join(ko)}." if ko else "")
                     + (f" Autres réponses : {', '.join(autres)}." if autres else ""))
        else:
            liste = ", ".join(f"{r['auteur']} ({hm(r['ts'])})" for r in rep)
            texte = (f"Appel des stations de {hm(a['debut'])} clos : "
                     f"{len(rep)} station(s) ont répondu" + (f" : {liste}." if rep else "."))
        op = self._store.get_config("operateur", "")
        self._store.add_journal("", op or "PC", texte, exercice=a.get("exercice") or None, emetteur=op or "PC",
                                nature="compte-rendu")
        return {"ok": True, "texte": texte}

    # ----- liaison et messages -----
    def tester_liaison(self, node_id):
        r = self._poller.tester_liaison(node_id, self._store.get_config("operateur", ""))
        self._noter_requete(node_id, "test liaison", r)
        return r

    def etalonner_batterie(self, pct):
        return self._poller.etalonner_batterie(pct)

    def chemin_contact(self, cle):
        return self._poller.chemin_contact(cle)

    def definir_chemin(self, cle, mode, relais=None):
        return self._poller.definir_chemin(cle, mode, relais or [], self._store.get_config("operateur", ""))

    def renvoyer_message(self, mid):
        return self._poller.renvoyer_message(int(mid))

    def _messages_avec_echos(self):
        """Pour nos messages de canal : répéteurs qui l'ont relayé (entendus par le nœud companion)."""
        msgs = self._store.list_messages(400)
        noms = None
        limite = time.time() - 2 * 86400
        norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
        for i, m in enumerate(msgs):
            if m.get("sens") != "emis" or m.get("canal") is None or (m.get("ts") or 0) < limite:
                continue
            # discussion : la station citée (@[nom]) a écrit sur le canal après notre message → livré à cette station
            cites = re.findall(r"@\[([^\]]+)\]", m.get("texte") or "")
            if cites:
                apres = [x for x in msgs[i + 1:] if x.get("sens") == "recu" and x.get("canal") == m.get("canal")
                         and (x.get("ts") or 0) <= (m.get("ts") or 0) + 7200]
                m["livreA"] = [c for c in cites if any(norm(x.get("auteur")) == norm(c) for x in apres)]
            e = self._resume_echos(f"{m.get('auteur')}: {m.get('texte')}", float(m.get("ts") or 0))
            if not e["echos"]:
                e["attente"] = time.time() - (m.get("ts") or 0) < 60
            m["echos"] = e
        return msgs

    def _resume_echos(self, texte_complet: str, ts: float, fenetre: float = 600) -> dict:
        """Répéteurs ayant relayé un de nos messages : directs (1er saut entendu) et tous les relais des échos."""
        ech = self._store.echos(texte_complet, ts, fenetre)
        if not ech:
            return {"directs": 0, "relais": 0, "noms": [], "tousNoms": [], "echos": 0, "details": []}
        noms = [((n.get("cle") or "").lower(), n.get("nom")) for n in self._store.list_nodes() if n.get("cle")]
        noms += [((c.get("public_key") or "").lower(), c.get("adv_name")) for c in self._poller._contacts.values()
                 if c.get("type") in (2, 3)]

        def nom(h):
            return next((nm for k, nm in noms if h and k.startswith(h)), f"relais {h.upper()}")
        directs, tous = {}, []
        for e in ech:
            t = max(1, int(e.get("taille") or 1))
            hops = [e["chemin"][i:i + 2 * t] for i in range(0, len(e.get("chemin") or ""), 2 * t)]
            if hops:
                directs.setdefault(hops[0], e.get("snr"))
                for h in hops:
                    if h not in tous:
                        tous.append(h)
        return {"directs": len(directs), "relais": len(tous), "noms": [nom(h) for h in directs],
                "tousNoms": [nom(h) for h in tous], "echos": len(ech),
                "details": [{"nom": nom(h), "snr": s} for h, s in directs.items()]}

    def historique(self, node_id, jours=7):
        return self._store.history(node_id, int(jours))

    def ports(self):
        return list_serial_ports()

    # ----- nœuds -----
    def enregistrer_noeud(self, doc, mot_de_passe=None):
        if not isinstance(doc, dict) or not str(doc.get("nom", "")).strip():
            return {"ok": False, "erreur": "Le nom du nœud est obligatoire."}
        doc = dict(doc)
        is_new = not doc.get("id")
        if is_new:
            doc["id"] = slug(doc["nom"])
            if self._store.get_node(doc["id"]) or self._store.find_by_name(doc["nom"]):
                return {"ok": False, "erreur": "Un nœud porte déjà ce nom."}
        else:
            old = self._store.get_node(doc["id"]) or {}
            # conserve les champs remplis automatiquement que le formulaire ne montre pas
            for k, v in old.items():
                doc.setdefault(k, v)
        doc.pop("aMotDePasse", None)
        saved = self._store.save_node(doc)
        if mot_de_passe is not None:
            self._store.set_password(saved["id"], mot_de_passe)
        return {"ok": True, "id": saved["id"]}

    def supprimer_noeud(self, node_id):
        self._store.delete_node(node_id)
        return {"ok": True}

    def supprimer_selection(self, ids, definitif=False):
        n = 0
        for nid in ids or []:
            node = self._store.get_node(str(nid))
            if not node:
                continue
            if definitif:
                self._store.delete_node(node["id"])
            else:
                node["statut"] = "supprime"
                self._store.save_node(node)
            n += 1
        op = self._store.get_config("operateur", "") or "PC"
        self._store.add_journal("", op, f"{n} nœud(s) {'effacé(s) définitivement' if definitif else 'passé(s) à l’état Supprimé'} depuis la liste.")
        return {"ok": True, "n": n}

    def restaurer_selection(self, ids):
        n = 0
        for nid in ids or []:
            node = self._store.get_node(str(nid))
            if node and node.get("statut") == "supprime":
                node["statut"] = "en-ligne"
                self._store.save_node(node)
                n += 1
        return {"ok": True, "n": n}

    def detail_message(self, mid):
        try:
            return self._poller.detail_message(int(mid))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("supervision").exception("detail_message")
            return {"ok": False, "erreur": str(e)}

    def supprimer_message(self, mid):
        self._store.delete_message(int(mid))
        return {"ok": True}

    def vider_conversation(self, canal=None, contact=None):
        return {"ok": True, "n": self._store.clear_conversation(canal, contact)}

    def marquer_verifie(self, node_id):
        self._store.patch_node(node_id, {"maj": now_iso()})
        return {"ok": True}

    # ----- journal -----
    def ajouter_journal(self, noeud, auteur, texte):
        texte = (texte or "").strip()
        if not texte:
            return {"ok": False, "erreur": "Le texte est vide."}
        self._store.add_journal(noeud, (auteur or "").strip().upper(), texte)
        self._store.set_config("operateur", (auteur or "").strip().upper())
        return {"ok": True}

    def supprimer_journal(self, entry_id):
        self._store.delete_journal(int(entry_id))
        return {"ok": True}

    def operateur(self):
        return self._store.get_config("operateur", "")

    # ----- liaison avec le nœud (USB ou Bluetooth) -----
    def connecter(self, cible, mode="usb", pin=None):
        if mode == "ble":
            self._store.set_config("pin_ble", pin or "")
        return self._poller.connect(cible, mode, pin)

    def chercher_bluetooth(self):
        return self._poller.scan_ble()

    def derniers_reglages(self):
        return {"mode": self._store.get_config("dernier_mode", "usb"),
                "port": self._store.get_config("dernier_port", ""),
                "ble": self._store.get_config("dernier_ble", ""),
                "pin": self._store.get_config("pin_ble", "")}

    def deconnecter(self):
        return self._poller.disconnect()

    def reprendre_surveillance(self, node_id):
        return self._poller.reprendre(node_id)

    def interroger(self):
        return self._poller.poll_now()

    def regler(self, intervalle=None, decouverte=None, heures=None):
        if intervalle is not None:
            self._poller.set_interval(int(intervalle), heures)
        if decouverte is not None:
            self._store.set_config("decouverte", bool(decouverte))
        self._store._bump()
        return {"ok": True}

    def dernier_port(self):
        return self._store.get_config("dernier_port", "")

    # ----- messagerie -----
    def envoyer_message(self, texte, canal=None, contact=None):
        return self._poller.envoyer_message(texte, canal, contact)

    # ----- commandes à distance -----
    LIBELLES = {"advert": "Envoyer un advert", "reboot": "Redémarrer", "poweroff": "Éteindre",
                "powersaving on": "Économie d'énergie : activer", "powersaving off": "Économie d'énergie : désactiver",
                "start ota": "Préparer la mise à jour (OTA)", "ver": "Vérifier la version", "clock sync": "Remettre l'heure",
                "clock": "Lire l'heure du nœud", "reinit chemin": "Réinitialiser le chemin",
                "test liaison": "Tester la liaison", "decouvrir voisins": "Découvrir les voisins"}

    def _noter_requete(self, node_id, cmd, r):
        try:
            c = (cmd or "").strip().lower()
            libelle = self.LIBELLES.get(c) or (f"Puissance TX {c.split()[-1]} dBm" if c.startswith("set tx")
                                                else f"Console : {cmd.strip()[8:]}" if c.startswith("console ") else c)
            r = r or {}
            resultat = (r.get("reponse") or r.get("message") or "").strip() if r.get("ok") else (r.get("erreur") or "échec")
            self._store.patch_node(node_id, {"derniereRequete": {
                "libelle": libelle, "date": now_iso(), "ok": bool(r.get("ok")),
                "resultat": resultat[:160] or ("sans réponse" if r.get("ok") else "échec"),
                "operateur": self._store.get_config("operateur", "")}})
        except Exception:  # noqa: BLE001
            logging.getLogger("supervision").exception("dernière requête")

    def reinit_chemin(self, node_id):
        r = self._poller.reinit_chemin(node_id, self._store.get_config("operateur", ""))
        self._noter_requete(node_id, "reinit chemin", r)
        return r

    def ajouter_contact(self, texte, nom="", ami=True):
        return self._poller.ajouter_contact(texte, nom, ami)

    def regions(self, lire_repeteurs=False):
        return self._poller.regions(bool(lire_repeteurs))

    def regler_region(self, canal=None, region="", defaut=False):
        return self._poller.regler_region(canal, region, bool(defaut))

    def ajouter_depuis_nom(self, nom, ami=False):
        return self._poller.ajouter_depuis_nom(nom, ami)

    def basculer_ami(self, cle, ami=None):
        return self._poller.basculer_ami(cle, ami)

    def decouvrir_voisins(self, node_id):
        r = self._poller.decouvrir_voisins(node_id, self._store.get_config("operateur", ""))
        self._noter_requete(node_id, "decouvrir voisins", r)
        return r

    def console(self, node_id, cmd):
        r = self._poller.console(node_id, cmd, self._store.get_config("operateur", ""))
        self._noter_requete(node_id, "console " + (cmd or "").strip(), r)
        return r

    def commande(self, node_id, cmd):
        r = self._poller.commande(node_id, cmd, self._store.get_config("operateur", ""))
        self._noter_requete(node_id, cmd, r)
        return r

    # ----- alertes et réglages -----
    def alertes_vues(self):
        self._store.mark_alerts_seen()
        return {"ok": True}

    def regler_options(self, options):
        o = options or {}
        if "seuilBatterie" in o:
            try:
                self._store.set_config("seuil_batterie", max(2.8, min(4.3, float(o["seuilBatterie"]))))
            except (TypeError, ValueError):
                pass
        if "reduireZone" in o:
            self._store.set_config("reduire_zone", bool(o["reduireZone"]))
        if "notifications" in o:
            self._store.set_config("notifications", bool(o["notifications"]))
        if "corrBattCompanion" in o:
            try:
                self._store.set_config("corr_batt_companion", max(-1.0, min(1.0, float(o["corrBattCompanion"]))))
                b = self._poller.batterie
                if b and b.get("brut"):
                    mv = self._poller._corr_batterie(b["brut"])
                    self._poller.batterie = {**b, "mv": mv, "pct": self._poller._pct_batterie(mv)}
            except (TypeError, ValueError):
                pass
        if "horlogeAuto" in o:
            self._store.set_config("horloge_auto", bool(o["horlogeAuto"]))
        if "fondCarte" in o:
            self._store.set_config("fond_carte", str(o["fondCarte"]))
        if "demarrageAuto" in o:
            if not systeme.regler_demarrage_auto(bool(o["demarrageAuto"])):
                self._store._bump()
                return {"ok": False, "erreur": "Le démarrage automatique n'a pas pu être modifié."}
        self._store._bump()
        return {"ok": True}

    # ----- exercices -----
    def demarrer_exercice(self, nom, type_="exercice", lieu="", autorite="", description=""):
        nom = (nom or "").strip()
        if not nom:
            return {"ok": False, "erreur": "Donnez un nom à l'exercice ou à l'intervention."}
        ex = self._store.start_exercise(nom, type_ or "exercice", self._store.get_config("operateur", ""),
                                        (lieu or "").strip(), (autorite or "").strip(), (description or "").strip())
        return {"ok": True, "exercice": ex}

    def terminer_exercice(self, bilan=None):
        ex = self._store.stop_exercise((bilan or "").strip() if bilan is not None else None)
        return {"ok": bool(ex), "exercice": ex}

    def modifier_exercice(self, ex_id, champs):
        self._store.update_exercise(int(ex_id), champs or {})
        return {"ok": True}

    # ----- main courante : saisie -----
    @staticmethod
    def _heure_iso(heure):
        """« 14:32 » (aujourd'hui) ou vide (maintenant) → date ISO locale."""
        from datetime import datetime
        h = (heure or "").strip()
        if not h:
            return now_iso()
        try:
            hh, mm = [int(x) for x in h.replace("h", ":").split(":")[:2]]
            return datetime.now().astimezone().replace(hour=hh, minute=mm, second=0, microsecond=0).isoformat(timespec="seconds")
        except (ValueError, TypeError):
            return now_iso()

    def mc_ajouter(self, heure, emetteur, destinataire, nature, texte, noeud=""):
        texte = (texte or "").strip()
        if not texte:
            return {"ok": False, "erreur": "Le message est vide."}
        ex = self._store.current_exercise()
        if not ex:
            return {"ok": False, "erreur": "Aucune main courante ouverte."}
        op = self._store.get_config("operateur", "")
        jid = self._store.add_journal(noeud or "", op or (emetteur or ""), texte, date=self._heure_iso(heure),
                                      exercice=ex["id"], emetteur=(emetteur or "").strip(),
                                      destinataire=(destinataire or "").strip(), nature=nature or "info")
        return {"ok": True, "id": jid}

    def mc_suite(self, jid, suite, clore=False):
        f = {"suite": (suite or "").strip()}
        if clore:
            f["cloture"] = now_iso()
        self._store.update_journal(int(jid), f)
        return {"ok": True}

    def mc_rouvrir(self, jid):
        self._store.update_journal(int(jid), {"cloture": ""})
        return {"ok": True}

    # ----- main courante : moyens engagés -----
    def moyen_ajouter(self, m):
        ex = self._store.current_exercise()
        if not ex:
            return {"ok": False, "erreur": "Aucune main courante ouverte."}
        if not (m or {}).get("indicatif") and not (m or {}).get("nom"):
            return {"ok": False, "erreur": "Indiquez au moins l'indicatif ou le nom."}
        mid = self._store.add_moyen(ex["id"], m)
        if (m or {}).get("arrive"):
            self._store.update_moyen(mid, {"arrivee": now_iso()})
        return {"ok": True, "id": mid}

    def moyen_modifier(self, mid, champs):
        self._store.update_moyen(int(mid), champs or {}, journal=False)
        return {"ok": True}

    def moyen_arrivee(self, mid):
        self._store.update_moyen(int(mid), {"arrivee": now_iso(), "depart": ""})
        return {"ok": True}

    def moyen_depart(self, mid):
        self._store.update_moyen(int(mid), {"depart": now_iso()})
        return {"ok": True}

    def moyen_supprimer(self, mid):
        self._store.delete_moyen(int(mid))
        return {"ok": True}

    # ----- APRS -----
    def regler_aprs(self, actif=None, liste=None, rayon=None):
        if not self._aprs:
            return {"ok": False, "erreur": "APRS indisponible."}
        return self._aprs.regler(actif, liste, rayon)

    # ----- statistiques & couverture -----
    def statistiques(self, node_id):
        return {"j7": self._store.availability(node_id, 7), "j30": self._store.availability(node_id, 30)}

    def test_couverture(self, lat, lon, lieu=""):
        return self._poller.test_couverture(lat, lon, lieu)

    def ligne_de_vue(self, lat, lon, h_ici=2, h_rep=8, externes=False):
        """Profil du terrain entre le point indiqué et chaque répéteur ayant une position."""
        try:
            lat, lon = float(str(lat).replace(",", ".")), float(str(lon).replace(",", "."))
            h_ici = max(0.0, min(300.0, float(str(h_ici or 0).replace(",", "."))))
            h_rep = max(0.0, min(300.0, float(str(h_rep or 0).replace(",", "."))))
        except (TypeError, ValueError):
            return {"ok": False, "erreur": "Latitude, longitude ou hauteur invalide."}
        cibles = []
        for n in self._store.list_nodes():
            if n.get("statut") in ("prevu", "supprime") or (n.get("proprio") == "externe" and not externes):
                continue
            try:
                nla, nlo = float(n.get("lat")), float(n.get("lon"))
            except (TypeError, ValueError):
                continue
            if abs(nla) < 0.01 and abs(nlo) < 0.01:
                continue
            d = visibilite.distance_m(lat, lon, nla, nlo)
            if d > 150000:
                continue
            cibles.append((d, n, nla, nlo))
        if not cibles:
            return {"ok": False, "erreur": "Aucun répéteur avec une position connue."}
        cibles.sort(key=lambda x: x[0])
        out = []
        for d, n, nla, nlo in cibles[:40]:
            try:
                p = visibilite.profil(lat, lon, h_ici, nla, nlo, h_rep)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "erreur": f"Altitudes du terrain indisponibles (connexion Internet nécessaire) : {e}"}
            out.append({"id": n.get("id"), "nom": n.get("nom"), "externe": n.get("proprio") == "externe",
                        "lat": nla, "lon": nlo, **p})
        return {"ok": True, "lat": lat, "lon": lon, "hIci": h_ici, "hRep": h_rep, "resultats": out}

    def liste_contacts(self):
        return self._poller.liste_contacts()

    def supprimer_couverture(self, cid):
        self._store.delete_coverage(int(cid))
        return {"ok": True}

    # ----- salle de crise -----
    def plein_ecran(self, actif):
        if not self._window:
            return {"ok": False}
        try:
            if bool(actif) != bool(getattr(self._window, "fullscreen", False)):
                self._window.toggle_fullscreen()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": str(e)}
        return {"ok": True}

    # ----- messages types -----
    MODELES_DEFAUT = [
        "[heure] [indicatif] arrivé sur zone, opérationnel.",
        "[heure] Point de situation : RAS, réseau opérationnel.",
        "[heure] [indicatif] demande renfort / moyens : ",
        "[heure] [indicatif] quitte la zone, fin de mission.",
        "[heure] Test réseau ADRASEC 06, merci d'accuser réception.",
        "[heure] Bien reçu, [indicatif].",
    ]

    def modeles(self):
        m = self._store.get_config("modeles", None)
        return m if isinstance(m, list) and m else list(self.MODELES_DEFAUT)

    def enregistrer_modeles(self, lignes):
        m = [str(x).strip() for x in (lignes or []) if str(x).strip()][:30]
        self._store.set_config("modeles", m or list(self.MODELES_DEFAUT))
        self._store._bump()
        return {"ok": True}

    # ----- sauvegarde -----
    def choisir_dossier_sauvegarde(self):
        if not self._window:
            return {"ok": False}
        r = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not r:
            return {"ok": False, "annule": True}
        d = r if isinstance(r, str) else r[0]
        self._store.set_config("dossier_sauvegarde", d)
        self._store._bump()
        return self.sauvegarder_maintenant()

    def sauvegarder_maintenant(self):
        d = self._store.get_config("dossier_sauvegarde", "")
        if not d:
            return {"ok": False, "erreur": "Choisissez d'abord un dossier de sauvegarde."}
        try:
            return {"ok": True, "chemin": self._store.backup(d)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Sauvegarde impossible : {e}"}

    # ----- mises à jour -----
    def verifier_maj(self):
        r = taches.verifier_maj(VERSION)
        if self._taches and r.get("ok"):
            self._taches.maj = r
        self._store._bump()
        return r

    def installer_maj(self):
        r = (self._taches.maj if self._taches else None) or taches.verifier_maj(VERSION)
        if not r.get("ok") or not r.get("disponible"):
            return {"ok": False, "erreur": "Aucune nouvelle version à installer."}
        return taches.installer_maj(r["url"], self._quitter or (lambda: None), r.get("taille"))

    def regler_maj_auto(self, actif):
        self._store.set_config("maj_auto", bool(actif))
        self._store._bump()
        return {"ok": True}

    # ----- rapports -----
    def _choisir(self, nom, filtre):
        if not self._window:
            return None
        path = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=nom, file_types=(filtre,))
        if not path:
            return None
        return path if isinstance(path, str) else path[0]

    def rapport(self, format_="pdf", voisins=False):
        jour = now_iso()[:16].replace(":", "h").replace("T", "-")
        try:
            if format_ == "xlsx":
                p = self._choisir(f"etat-reseau-adrasec06-{jour}.xlsx", "Classeur Excel (*.xlsx)")
                if not p:
                    return {"ok": False, "annule": True}
                rapports.excel_etat(self._store, p, bool(voisins))
            else:
                p = self._choisir(f"etat-reseau-adrasec06-{jour}.pdf", "Document PDF (*.pdf)")
                if not p:
                    return {"ok": False, "annule": True}
                rapports.pdf_etat(self._store, p, bool(voisins))
        except Exception as e:  # noqa: BLE001
            logging.exception("rapport")
            return {"ok": False, "erreur": f"Rapport impossible : {e}"}
        return {"ok": True, "chemin": p}

    def main_courante(self, ex_id):
        ex = self._store.get_exercise(int(ex_id))
        if not ex:
            return {"ok": False, "erreur": "Exercice introuvable."}
        p = self._choisir(f"main-courante-{slug(ex['nom'])}.pdf", "Document PDF (*.pdf)")
        if not p:
            return {"ok": False, "annule": True}
        try:
            rapports.pdf_main_courante(self._store, ex["id"], p)
        except Exception as e:  # noqa: BLE001
            logging.exception("main courante")
            return {"ok": False, "erreur": f"Main courante impossible : {e}"}
        return {"ok": True, "chemin": p}

    # ----- suppression et archivage des mains courantes -----
    def supprimer_main_courante(self, ex_id):
        ex = self._store.get_exercise(int(ex_id))
        if not ex:
            return {"ok": False, "erreur": "Main courante introuvable."}
        if not ex.get("fin"):
            return {"ok": False, "erreur": "Clôturez d'abord cette main courante avant de la supprimer."}
        self._store.delete_exercise(ex["id"])
        self._store.add_journal("", self._store.get_config("operateur", "") or "PC",
                                f"Main courante « {ex['nom']} » supprimée.", exercice=None)
        return {"ok": True}

    def choisir_dossier_archives(self):
        if not self._window:
            return {"ok": False}
        r = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not r:
            return {"ok": False, "annule": True}
        self._store.set_config("dossier_archives", r if isinstance(r, str) else r[0])
        self._store._bump()
        return {"ok": True}

    def archiver_main_courante(self, ex_id):
        ex = self._store.get_exercise(int(ex_id))
        if not ex:
            return {"ok": False, "erreur": "Main courante introuvable."}
        if not ex.get("fin"):
            return {"ok": False, "erreur": "Clôturez d'abord cette main courante avant de l'archiver."}
        dossier = self._store.get_config("dossier_archives", "") or ""
        if not dossier or not Path(dossier).is_dir():
            r = self.choisir_dossier_archives()
            if not r.get("ok"):
                return {"ok": False, "annule": True}
            dossier = self._store.get_config("dossier_archives", "")
        jour = str(ex.get("debut") or now_iso())[:10]
        base = Path(dossier) / f"MainCourante-{jour}-{slug(ex['nom'])}"
        try:
            rapports.pdf_main_courante(self._store, ex["id"], str(base) + ".pdf")
            donnees = {"exercice": ex, "registre": self._store.journal_of_exercise(ex["id"]),
                       "moyens": self._store.list_moyens(ex["id"])}
            Path(str(base) + ".json").write_text(json.dumps(donnees, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logging.getLogger("supervision").exception("archivage")
            return {"ok": False, "erreur": f"Archivage impossible : {e}"}
        self._store.update_exercise(ex["id"], {"archive": str(base) + ".pdf", "archiveDate": now_iso()})
        return {"ok": True, "chemin": str(base) + ".pdf"}

    def desarchiver_main_courante(self, ex_id):
        self._store.update_exercise(int(ex_id), {"archive": "", "archiveDate": ""})
        return {"ok": True}

    def ouvrir_dossier_archives(self):
        d = self._store.get_config("dossier_archives", "") or ""
        if not d:
            return {"ok": False, "erreur": "Aucun dossier d'archives choisi."}
        try:
            os.startfile(d)  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": str(e)}

    # ----- analyse du nœud companion -----
    def analyser_companion(self):
        return self._poller.analyser_companion()

    # ----- fichiers -----
    def exporter(self):
        if not self._window:
            return {"ok": False}
        name = f"supervision-adrasec06-{now_iso()[:10]}.json"
        path = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name,
                                               file_types=("Export supervision (*.json)",))
        if not path:
            return {"ok": False, "annule": True}
        path = path if isinstance(path, str) else path[0]
        Path(path).write_text(json.dumps(self._store.export_data(), ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True, "chemin": path}

    def importer(self):
        if not self._window:
            return {"ok": False}
        paths = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=("Export supervision (*.json)",))
        if not paths:
            return {"ok": False, "annule": True}
        try:
            data = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
            n, j = self._store.import_data(data)
        except (OSError, ValueError) as e:
            return {"ok": False, "erreur": str(e)}
        return {"ok": True, "noeuds": n, "journal": j}


def main():
    store = Store()
    window_ref = {}
    tray_ref = {}

    courrier = courriel.Courrier(store)

    def alerte(niveau, noeud, texte):
        store.add_alert(niveau, noeud, texte)
        try:
            courrier.alerte(niveau, noeud, texte)
        except Exception:  # noqa: BLE001
            pass
        if store.get_config("notifications", True):
            titre = {"critique": "Répéteur hors ligne", "attention": "Alerte batterie",
                     "message": "Message MeshCore", "info": "Supervision ADRASEC 06"}.get(niveau, "Supervision ADRASEC 06")
            systeme.notifier(titre, texte)

    poller = Poller(store, alerte=alerte)
    api = Api(store, poller)
    api._taches = taches.Taches(store, alerte, VERSION)
    api._aprs = aprs.AprsIS(store, VERSION)
    api._courrier = courrier
    api._web = web.Tableau(store, api.tableau_donnees)
    api._resilience = resilience.TestResilience(store, poller, api, alerte)
    html = resource_path("ui/index.html")
    reduit = "--reduit" in sys.argv
    window = webview.create_window(
        f"Supervision MeshCore ADRASEC 06 — v{VERSION}", url=str(html), js_api=api,
        width=1360, height=860, min_size=(800, 560), background_color="#0f161d", hidden=reduit,
        maximized=True,
    )
    api._window = window
    window_ref["w"] = window
    state = {"quitter": False}

    def afficher():
        try:
            window.show()
            window.maximize()
        except Exception:
            pass

    def quitter():
        state["quitter"] = True
        poller.disconnect()
        if tray_ref.get("t"):
            tray_ref["t"].arreter()
        try:
            window.destroy()
        except Exception:
            pass

    api._quitter = quitter

    def on_closing():
        tray = tray_ref.get("t")
        if not state["quitter"] and tray and tray.active and store.get_config("reduire_zone", True):
            window.hide()
            systeme.notifier("Supervision ADRASEC 06", "La supervision continue en arrière-plan. "
                             "Icône près de l'horloge pour la rouvrir ou quitter.")
            return False
        poller.disconnect()
        if tray:
            tray.arreter()
        return True

    def on_start():
        tray_ref["t"] = systeme.IconeZone(resource_path("icone.png"), afficher, poller.poll_now, quitter)
        # reconnexion automatique au dernier nœud utilisé (utile au démarrage avec Windows)
        if store.get_config("reconnexion_auto", True):
            mode = store.get_config("dernier_mode", "")
            cible = store.get_config("dernier_ble" if mode == "ble" else "dernier_port", "")
            if mode and (cible or mode == "ble") and reduit:
                poller.connect(cible, mode, store.get_config("pin_ble", "") or None)

    window.events.closing += on_closing
    webview.start(on_start, debug="--debug" in sys.argv)
    # fermeture complète : l'icône près de l'horloge et les tâches de fond ne doivent pas garder le processus en vie
    try:
        poller.disconnect()
    except Exception:
        pass
    if tray_ref.get("t"):
        tray_ref["t"].arreter()
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
