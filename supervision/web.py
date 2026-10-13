"""Tableau de bord en lecture seule, consultable depuis une tablette ou un téléphone du réseau local.

http://<adresse du PC>:<port>/  — carte, répéteurs ADRASEC, alertes, canal #ADRASEC 06, appel des stations.
Aucune commande n'est possible depuis cette page. Un code d'accès peut être exigé.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import Store, resource_path

log = logging.getLogger("supervision")

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json"}


def adresses_locales() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # aucune donnée n'est envoyée : sert à connaître l'interface réseau utilisée
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


class Tableau:
    def __init__(self, store: Store, fournisseur):
        self.store = store
        self.fournisseur = fournisseur  # fonction → dict des données affichées
        self.srv = None
        self.etat = {"actif": False, "message": "Désactivé", "adresses": []}
        self.redemarrer()

    def reglages(self) -> dict:
        return {"actif": bool(self.store.get_config("web_actif", False)),
                "port": int(self.store.get_config("web_port", 8060) or 8060),
                "code": self.store.get_config("web_code", "") or ""}

    def regler(self, actif=None, port=None, code=None) -> dict:
        if actif is not None:
            self.store.set_config("web_actif", bool(actif))
        if port is not None:
            try:
                p = int(port)
                if 1024 <= p <= 65535:
                    self.store.set_config("web_port", p)
            except (TypeError, ValueError):
                pass
        if code is not None:
            self.store.set_config("web_code", "".join(ch for ch in str(code) if ch.isalnum())[:20])
        self.redemarrer()
        return {"ok": True, **self.reglages(), "etat": self.etat}

    def arreter(self):
        if self.srv is not None:
            try:
                self.srv.shutdown()
                self.srv.server_close()
            except Exception:  # noqa: BLE001
                pass
        self.srv = None

    def redemarrer(self):
        self.arreter()
        r = self.reglages()
        if not r["actif"]:
            self.etat = {"actif": False, "message": "Désactivé", "adresses": []}
            self.store._bump()
            return
        tableau = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # pas de journal console
                pass

            def _code_ok(self, q) -> bool:
                code = tableau.reglages()["code"]
                if not code:
                    return True
                if q.get("code", [""])[0] == code:
                    return True
                c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
                return c.get("adrasec_code") is not None and c["adrasec_code"].value == code

            def _envoyer(self, code, corps: bytes, ctype: str, extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(corps)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(corps)

            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                q = parse_qs(u.query)
                chemin = u.path
                if chemin.startswith("/leaflet/") or chemin in ("/logo.png",):
                    base = resource_path("ui").resolve()
                    f = (base / chemin.lstrip("/")).resolve()
                    if base in f.parents and f.is_file():
                        return self._envoyer(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))
                    return self._envoyer(404, b"", "text/plain")
                if not self._code_ok(q):
                    page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                            "<title>ADRASEC 06</title><body style='font-family:system-ui;background:#0f161d;color:#e6edf3;"
                            "display:grid;place-items:center;min-height:90vh'><form><p>Code d'accès au tableau de bord</p>"
                            "<input name=code autofocus style='font-size:1.4rem;padding:8px'> <button>Entrer</button></form>")
                    return self._envoyer(401, page.encode(), TYPES[".html"])
                extra = {}
                code = tableau.reglages()["code"]
                if code and q.get("code"):
                    extra["Set-Cookie"] = f"adrasec_code={code}; Path=/; SameSite=Strict; Max-Age=2592000"
                if chemin in ("/", "/index.html"):
                    f = resource_path("ui/tableau.html")
                    return self._envoyer(200, f.read_bytes(), TYPES[".html"], extra)
                if chemin == "/api/etat":
                    try:
                        data = json.dumps(tableau.fournisseur(), ensure_ascii=False, default=str).encode("utf-8")
                    except Exception as e:  # noqa: BLE001
                        log.exception("tableau de bord")
                        data = json.dumps({"erreur": str(e)}).encode()
                    return self._envoyer(200, data, TYPES[".json"], extra)
                return self._envoyer(404, b"Page inconnue", "text/plain")

        try:
            self.srv = ThreadingHTTPServer(("0.0.0.0", r["port"]), H)
            self.srv.daemon_threads = True
        except OSError as e:
            self.srv = None
            self.etat = {"actif": False, "message": f"Port {r['port']} indisponible ({e}). Choisissez un autre port.",
                         "adresses": []}
            self.store._bump()
            return
        threading.Thread(target=self.srv.serve_forever, name="tableau-web", daemon=True).start()
        adr = [f"http://{ip}:{r['port']}/" for ip in adresses_locales()] or [f"http://localhost:{r['port']}/"]
        self.etat = {"actif": True, "message": "En service", "adresses": adr}
        log.info("Tableau de bord web : %s", ", ".join(adr))
        self.store._bump()
