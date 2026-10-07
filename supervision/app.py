"""Supervision MeshCore ADRASEC 06 — programme de bureau (Windows)."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import webview

from .poller import Poller, list_serial_ports
from .store import Store, data_dir, now_iso, resource_path, slug

VERSION = "1.0.0"

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
    poller = Poller(store)
    api = Api(store, poller)
    html = resource_path("ui/index.html")
    window = webview.create_window(
        f"Supervision MeshCore ADRASEC 06 — v{VERSION}", url=str(html), js_api=api,
        width=1360, height=900, min_size=(900, 640), background_color="#eef1f3",
    )
    api._window = window

    def on_closing():
        poller.disconnect()

    window.events.closing += on_closing
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
