"""Alertes par e-mail (SMTP) : répéteur hors ligne, batterie faible, alerte précoce, rapport hebdomadaire.

Les alertes qui arrivent ensemble sont regroupées dans un seul e-mail (attente de 60 s).
"""
from __future__ import annotations

import logging
import mimetypes
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .store import Store

log = logging.getLogger("supervision")

DEFAUTS = {"serveur": "mail.infomaniak.com", "port": 465, "securite": "ssl"}
TITRES = {"critique": "Hors ligne", "attention": "Attention", "message": "Message reçu", "info": "Information"}


def reglages(store: Store, avec_mdp: bool = False) -> dict:
    r = {
        "actif": bool(store.get_config("mail_actif", False)),
        "serveur": store.get_config("smtp_serveur", DEFAUTS["serveur"]) or DEFAUTS["serveur"],
        "port": int(store.get_config("smtp_port", DEFAUTS["port"]) or DEFAUTS["port"]),
        "securite": store.get_config("smtp_securite", DEFAUTS["securite"]) or DEFAUTS["securite"],
        "utilisateur": store.get_config("smtp_utilisateur", "") or "",
        "expediteur": store.get_config("mail_de", "") or "",
        "destinataires": store.get_config("mail_a", "") or "",
        "messages": bool(store.get_config("mail_messages", False)),
        "aMotDePasse": bool(store.get_config("smtp_mdp", "")),
    }
    if avec_mdp:
        r["mdp"] = store.get_config("smtp_mdp", "") or ""
    return r


def regler(store: Store, o: dict) -> dict:
    o = o or {}
    cles = {"actif": "mail_actif", "serveur": "smtp_serveur", "port": "smtp_port", "securite": "smtp_securite",
            "utilisateur": "smtp_utilisateur", "expediteur": "mail_de", "destinataires": "mail_a", "messages": "mail_messages"}
    for k, ck in cles.items():
        if k in o:
            v = o[k]
            if k == "port":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            elif k in ("actif", "messages"):
                v = bool(v)
            else:
                v = str(v or "").strip()
            store.set_config(ck, v)
    if o.get("mdp"):
        store.set_config("smtp_mdp", str(o["mdp"]))
    store._bump()
    return {"ok": True, **reglages(store)}


def _destinataires(r: dict) -> list[str]:
    return [x.strip() for x in str(r.get("destinataires") or "").replace(";", ",").split(",") if "@" in x]


def envoyer(store: Store, sujet: str, corps: str, pieces: list[str] | None = None) -> dict:
    r = reglages(store, avec_mdp=True)
    dest = _destinataires(r)
    if not dest:
        return {"ok": False, "erreur": "Indiquez au moins un destinataire."}
    if not r["serveur"]:
        return {"ok": False, "erreur": "Indiquez le serveur d'envoi (SMTP)."}
    exp = r["expediteur"] or r["utilisateur"]
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = f"Supervision ADRASEC 06 <{exp}>" if exp else "Supervision ADRASEC 06"
    msg["To"] = ", ".join(dest)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="adrasec06.local")
    msg.set_content(corps)
    for p in pieces or []:
        try:
            data = Path(p).read_bytes()
            ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=Path(p).name)
        except OSError as e:
            log.warning("pièce jointe %s : %s", p, e)
    try:
        ctx = ssl.create_default_context()
        if r["securite"] == "ssl":
            srv = smtplib.SMTP_SSL(r["serveur"], r["port"], timeout=30, context=ctx)
        else:
            srv = smtplib.SMTP(r["serveur"], r["port"], timeout=30)
            if r["securite"] == "starttls":
                srv.starttls(context=ctx)
        with srv:
            if r["utilisateur"]:
                srv.login(r["utilisateur"], r["mdp"])
            srv.send_message(msg)
        return {"ok": True}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "erreur": "Identifiant ou mot de passe refusé par le serveur."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erreur": f"Envoi impossible : {e}"}


class Courrier:
    """File d'attente : regroupe les alertes reçues en 60 s dans un seul e-mail."""

    def __init__(self, store: Store):
        self.store = store
        self._file: list[tuple[float, str, str, str]] = []
        self._lock = threading.Lock()
        self._evt = threading.Event()
        self.dernier = {"date": None, "ok": None, "erreur": ""}
        threading.Thread(target=self._boucle, name="courriel", daemon=True).start()

    def alerte(self, niveau: str, noeud: str, texte: str):
        r = reglages(self.store)
        if not r["actif"] or not _destinataires(r):
            return
        if niveau == "info":
            return
        if niveau == "message" and not r["messages"]:
            return
        with self._lock:
            self._file.append((time.time(), niveau, noeud or "", texte))
        self._evt.set()

    def _boucle(self):
        while True:
            self._evt.wait()
            time.sleep(60)  # regroupement
            with self._lock:
                lot, self._file = self._file, []
                self._evt.clear()
            if not lot:
                continue
            graves = [x for x in lot if x[1] == "critique"]
            sujet = ("[ADRASEC 06] " + (f"{len(graves)} répéteur(s) hors ligne" if graves else "Alerte réseau MeshCore")
                     + (f" (+{len(lot) - len(graves)} autre(s))" if graves and len(lot) > len(graves) else ""))
            if len(lot) == 1:
                sujet = f"[ADRASEC 06] {TITRES.get(lot[0][1], 'Alerte')} : {lot[0][3][:80]}"
            lignes = [f"{time.strftime('%d/%m/%Y %H:%M', time.localtime(t))}  [{TITRES.get(n, n)}]  {tx}"
                      for t, n, _no, tx in lot]
            corps = ("Supervision du réseau MeshCore ADRASEC 06\n\n" + "\n".join(lignes)
                     + "\n\n— Message automatique du logiciel de supervision.")
            r = envoyer(self.store, sujet, corps)
            self.dernier = {"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": r["ok"], "erreur": r.get("erreur", "")}
            if not r["ok"]:
                log.warning("Alerte e-mail : %s", r.get("erreur"))
            self.store._bump()
