"""Supervision MeshCore ADRASEC 06 — programme de bureau (Windows)."""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import webview

from . import rapports, systeme, taches
from .poller import Poller, list_serial_ports
from .store import Store, data_dir, now_iso, resource_path, slug

VERSION = "1.3.2"

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
            "messages": self._store.list_messages(400),
            "alertes": self._store.list_alerts(30),
            "exercice": self._store.current_exercise(),
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
                "modeles": self.modeles(),
            },
            "maj": self._taches.maj if self._taches else None,
            "traces": self._store.tracks(6),
            "couvertures": self._store.list_coverage(),
            "dispo": {n["id"]: {"j7": self._store.availability(n["id"], 7)["pct"],
                                "j30": self._store.availability(n["id"], 30)["pct"]}
                      for n in nodes if n.get("proprio") != "externe" and n.get("statut") not in ("prevu", "supprime")},
        }

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

    def interroger(self):
        return self._poller.poll_now()

    def regler(self, intervalle=None, decouverte=None):
        if intervalle is not None:
            self._poller.set_interval(int(intervalle))
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
    def commande(self, node_id, cmd):
        return self._poller.commande(node_id, cmd, self._store.get_config("operateur", ""))

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
        if "fondCarte" in o:
            self._store.set_config("fond_carte", str(o["fondCarte"]))
        if "demarrageAuto" in o:
            if not systeme.regler_demarrage_auto(bool(o["demarrageAuto"])):
                self._store._bump()
                return {"ok": False, "erreur": "Le démarrage automatique n'a pas pu être modifié."}
        self._store._bump()
        return {"ok": True}

    # ----- exercices -----
    def demarrer_exercice(self, nom, type_="exercice"):
        nom = (nom or "").strip()
        if not nom:
            return {"ok": False, "erreur": "Donnez un nom à l'exercice ou à l'intervention."}
        ex = self._store.start_exercise(nom, type_ or "exercice", self._store.get_config("operateur", ""))
        return {"ok": True, "exercice": ex}

    def terminer_exercice(self):
        ex = self._store.stop_exercise()
        return {"ok": bool(ex), "exercice": ex}

    # ----- statistiques & couverture -----
    def statistiques(self, node_id):
        return {"j7": self._store.availability(node_id, 7), "j30": self._store.availability(node_id, 30)}

    def test_couverture(self, lat, lon, lieu=""):
        return self._poller.test_couverture(lat, lon, lieu)

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
        return taches.installer_maj(r["url"], self._quitter or (lambda: None))

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

    def alerte(niveau, noeud, texte):
        store.add_alert(niveau, noeud, texte)
        if store.get_config("notifications", True):
            titre = {"critique": "Répéteur hors ligne", "attention": "Alerte batterie",
                     "message": "Message MeshCore", "info": "Supervision ADRASEC 06"}.get(niveau, "Supervision ADRASEC 06")
            systeme.notifier(titre, texte)

    poller = Poller(store, alerte=alerte)
    api = Api(store, poller)
    api._taches = taches.Taches(store, alerte, VERSION)
    html = resource_path("ui/index.html")
    reduit = "--reduit" in sys.argv
    window = webview.create_window(
        f"Supervision MeshCore ADRASEC 06 — v{VERSION}", url=str(html), js_api=api,
        width=1360, height=900, min_size=(900, 640), background_color="#0f161d", hidden=reduit,
    )
    api._window = window
    window_ref["w"] = window
    state = {"quitter": False}

    def afficher():
        try:
            window.show()
            window.restore()
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
