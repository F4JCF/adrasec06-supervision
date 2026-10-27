"""Lecture automatique du réseau via un nœud MeshCore « companion » relié en USB ou en Bluetooth.

Le nœud doit être flashé en firmware *Companion USB (serial)* ou *Companion Bluetooth*. Il sert de passerelle :
le logiciel lit ses contacts (tous les répéteurs qu'il a entendus), puis, pour chaque
répéteur ADRASEC, se connecte avec le mot de passe administrateur et demande :
  - le statut (bruit, erreurs RX, batterie, uptime, paquets) ;
  - la liste des voisins avec leur SNR ;
  - la version du firmware (commande « ver ») et la puissance (« get tx »).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import hashlib
import random
import re
import threading
import time
from collections import deque

from .store import Store, now_iso

log = logging.getLogger("supervision")

try:  # la bibliothèque est optionnelle pour que l'interface s'ouvre même sans elle
    from meshcore import MeshCore, EventType
except Exception:  # pragma: no cover
    MeshCore = None
    EventType = None

ROLE_BY_TYPE = {2: "repeteur", 3: "room"}
FAILS_DEGRADE = 1
FAILS_OFFLINE = 3


def list_serial_ports() -> list[dict]:
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    ports = []
    for p in list_ports.comports():
        desc = p.description or ""
        hint = any(k in desc.upper() for k in ("CP210", "CH340", "CH910", "USB", "SERIAL", "JTAG"))
        ports.append({"port": p.device, "description": desc, "probable": hint})
    ports.sort(key=lambda x: (not x["probable"], x["port"]))
    return ports


class Poller:
    def __init__(self, store: Store, alerte=None):
        self.store = store
        self.alerte = alerte or (lambda niveau, noeud, texte: None)
        self.canaux: list[dict] = []
        self.equipes: list[dict] = []
        self.position = None
        self.batterie = None  # batterie du nœud companion : {"mv", "pct", "date"}
        self.analyse_companion = None
        self._batt_alerte = False
        self._eq_task = None
        self.couverture_en_cours = False
        self.prioritaire = 0
        self._recalcul = False  # demandes de l'opérateur (test, commande) en attente : l'interrogation leur cède la place
        self._secrets: dict[int, str] = {}  # clés secrètes des canaux (jamais envoyées au tableau de bord web)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="meshcore", daemon=True)
        self.thread.start()
        self._battement = time.time()
        self.console_etape = ""
        asyncio.run_coroutine_threadsafe(self._coeur(), self.loop)
        self.mc = None
        self.port = None
        self.mode = None
        self.local_name = ""
        self.radio = {}
        self.busy = False
        self.last_cycle = None
        self.next_cycle = None
        self.message = "Non connecté"
        self.events = deque(maxlen=200)
        self._task = None
        self._wake = None
        self._contacts: dict = {}

    # ---------- boucle asyncio ----------
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _coeur(self):
        """Battement toutes les 2 s : s'il s'arrête, la boucle de la liaison radio est bloquée."""
        while True:
            self._battement = time.time()
            await asyncio.sleep(2)

    def _submit(self, coro, timeout=None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout) if timeout else fut

    def _log(self, txt: str):
        self.events.appendleft({"date": now_iso(), "texte": txt})
        log.info(txt)
        self.store._bump()

    # ---------- API utilisée par l'interface ----------
    def state(self) -> dict:
        return {
            "bibliotheque": MeshCore is not None,
            "connecte": self.mc is not None,
            "consoleEtape": getattr(self, "console_etape", ""),
            "boucleBloquee": round(time.time() - getattr(self, "_battement", time.time())) if time.time() - getattr(self, "_battement", time.time()) > 15 else 0,
            "port": self.port,
            "mode": self.mode,
            "noeudLocal": self.local_name,
            "radio": self.radio if self.mc is not None else {},
            "occupe": self.busy,
            "dernierCycle": self.last_cycle,
            "prochainCycle": self.next_cycle,
            "message": self.message,
            "intervalle": self.interval(),
            "heures": self.heures_fixes(),
            "decouverte": bool(self.store.get_config("decouverte", True)),
            "evenements": list(self.events)[:60],
            "canaux": self.canaux if self.mc is not None else [],
            "equipes": self.equipes,
            "position": self.position,
            "batterie": self.batterie if self.mc is not None else None,
            "regions": self.etat_regions(),
            "analyseCompanion": self.analyse_companion if self.mc is not None else None,
            "couvertureEnCours": self.couverture_en_cours,
            "contactsMessagerie": self.contacts_messagerie() if self.mc is not None else [],
            "amisAttente": self.store.get_config("amis_attente", []) or [],
        }

    def interval(self) -> int:
        """Intervalle en minutes ; 0 = collecte à heures fixes."""
        v = self.store.get_config("intervalle_min", 15)
        return 0 if v in (0, "0") else int(v or 15)

    def heures_fixes(self) -> list[str]:
        return [h for h in (self.store.get_config("heures_collecte", []) or []) if re.fullmatch(r"\d{2}:\d{2}", str(h))]

    def _attente(self) -> float:
        """Secondes jusqu'à la prochaine collecte."""
        m = self.interval()
        heures = self.heures_fixes()
        if m == 0 and heures:
            from datetime import datetime, timedelta
            now = datetime.now()
            cands = []
            for h in heures:
                hh, mm = int(h[:2]), int(h[3:])
                t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if t <= now + timedelta(seconds=20):
                    t += timedelta(days=1)
                cands.append(t)
            return max(30.0, (min(cands) - now).total_seconds())
        return max(1, m or 60) * 60

    def connect(self, cible: str, mode: str = "usb", pin: str | None = None) -> dict:
        if MeshCore is None:
            return {"ok": False, "erreur": "La bibliothèque meshcore n'est pas installée."}
        try:
            return self._submit(self._connect(cible, mode, pin), timeout=60)
        except Exception as e:  # noqa: BLE001
            quoi = "Bluetooth" if mode == "ble" else cible
            return {"ok": False, "erreur": f"Connexion impossible ({quoi}) : {e}"}

    def scan_ble(self, duree: float = 10.0) -> dict:
        try:
            return {"ok": True, "noeuds": self._submit(self._scan_ble(duree), timeout=duree + 10)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Recherche Bluetooth impossible : {e}. "
                                           "Vérifiez que le Bluetooth du PC est activé.", "noeuds": []}

    async def _scan_ble(self, duree: float) -> list[dict]:
        """Liste tous les appareils Bluetooth LE à portée, les nœuds MeshCore en tête.

        Windows ne transmet pas toujours le nom annoncé pendant la recherche : on garde donc
        tous les appareils, en repérant les MeshCore par leur nom ou par le service UART Nordic."""
        from bleak import BleakScanner
        uart = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
        found = await BleakScanner.discover(timeout=duree, return_adv=True)
        out = []
        for _addr, (dev, adv) in found.items():
            name = adv.local_name or dev.name or ""
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            low = name.lower()
            meshcore = (low.startswith("meshcore") or uart in uuids
                        or any(k in low for k in ("wio tracker", "t1000", "sensecap", "heltec", "rak", "lilygo", "t-echo", "t-beam", "xiao")))
            out.append({"adresse": dev.address, "nom": name.replace("MeshCore-", "", 1) if name else "(sans nom)",
                        "rssi": adv.rssi, "meshcore": meshcore, "uart": uart in uuids})
            log.info("BLE vu : %s | nom=%r | rssi=%s | services=%s", dev.address, name, adv.rssi, uuids)
        log.info("Recherche Bluetooth : %d appareil(s), dont %d MeshCore probable(s)",
                 len(out), sum(1 for x in out if x["meshcore"]))
        out.sort(key=lambda x: (not x["meshcore"], x["nom"] == "(sans nom)", -(x["rssi"] or -999)))
        return out

    def disconnect(self) -> dict:
        try:
            self._submit(self._disconnect(), timeout=15)
        except Exception:
            pass
        return {"ok": True}

    def poll_now(self) -> dict:
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        if self._wake is not None:
            self.loop.call_soon_threadsafe(self._wake.set)
        return {"ok": True}

    def set_interval(self, minutes: int, heures=None):
        self.store.set_config("intervalle_min", 0 if int(minutes) == 0 else max(1, int(minutes)))
        if heures is not None:
            propres = sorted({f"{int(h):02d}:{m}" for h, m in re.findall(r"\b([01]?\d|2[0-3])\s*[:hH]\s*([0-5]\d)\b", str(heures))})
            self.store.set_config("heures_collecte", propres)
        if self.mc is not None and self._wake is not None:
            # recalcule la prochaine échéance sans lancer de collecte immédiate
            self._recalcul = True
            self.loop.call_soon_threadsafe(self._wake.set)

    # ---------- connexion ----------
    async def _connect(self, cible: str, mode: str = "usb", pin: str | None = None) -> dict:
        await self._disconnect()
        ble = mode == "ble"
        self.message = "Recherche et connexion Bluetooth…" if ble else f"Connexion à {cible}…"
        self.store._bump()
        if ble:
            mc = await MeshCore.create_ble(address=cible or None, pin=(pin or None),
                                           auto_reconnect=True, max_reconnect_attempts=5)
        else:
            mc = await MeshCore.create_serial(cible, 115200, auto_reconnect=True, max_reconnect_attempts=5)
        if mc is None:
            self.message = "Non connecté"
            if ble:
                return {"ok": False, "erreur": "Aucun nœud MeshCore n'a répondu en Bluetooth. Vérifiez qu'il est flashé "
                                               "en « Companion Bluetooth », allumé, proche du PC et non connecté au téléphone."}
            return {"ok": False, "erreur": f"Pas de réponse d'un nœud MeshCore companion sur {cible}. "
                                           "Vérifiez que le nœud est flashé en « Companion USB »."}
        self.mc = mc
        self.mode = mode
        self.port = ("Bluetooth " + cible) if ble and cible else ("Bluetooth" if ble else cible)
        try:
            ev = await mc.commands.send_appstart()
            if ev and ev.type != EventType.ERROR:
                p = ev.payload or {}
                self.local_name = (p.get("name") or "").strip("\x00 ")
                self.radio = {k: p.get(k) for k in ("radio_freq", "radio_bw", "radio_sf", "radio_cr", "tx_power")}
                if abs(p.get("adv_lat") or 0) > 0.01 and abs(p.get("adv_lon") or 0) > 0.01:
                    self.position = {"lat": p["adv_lat"], "lon": p["adv_lon"]}
        except Exception:
            pass
        try:  # le nœud companion prend l'heure du PC : c'est elle que « clock sync » transmet aux répéteurs
            await mc.commands.set_time(int(time.time()))
        except Exception:  # noqa: BLE001
            pass
        try:  # et son écran suit l'heure légale (hiver / été)
            fz = await self._fuseau_ecran()
            if fz and fz[0] != fz[1]:
                self._log(f"Écran du companion : fuseau corrigé UTC{fz[0]:+d} → UTC{fz[1]:+d} (changement d'heure)")
        except Exception:  # noqa: BLE001
            pass
        try:
            mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
            mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)
            mc.subscribe(EventType.RX_LOG_DATA, self._on_rx_log)
            mc.set_decrypt_channel_logs(True)  # pour retrouver le parcours des messages de canal
        except Exception as e:  # noqa: BLE001
            log.warning("Abonnement aux messages impossible : %s", e)
        try:
            await mc.start_auto_message_fetching()
        except Exception:
            pass
        await self._load_channels()
        await self._maj_position()
        await self._maj_batterie()
        try:
            res = await mc.commands.get_contacts()
            if res is not None and res.type != EventType.ERROR:
                self._contacts = dict(res.payload or {})
        except Exception:
            pass
        self.store.set_config("dernier_mode", mode)
        self.store.set_config("dernier_ble" if ble else "dernier_port", cible)
        self.message = f"Connecté à {self.local_name or 'nœud'} ({self.port})"
        self._log(f"Connexion au nœud {self.local_name or ''} par {'Bluetooth' if ble else 'USB sur ' + cible}")
        self._wake = asyncio.Event()
        self._task = asyncio.ensure_future(self._cycle_loop())
        self._eq_task = asyncio.ensure_future(self._equipes_loop())
        return {"ok": True}

    async def _disconnect(self):
        if self._task:
            self._task.cancel()
            self._task = None
        if self._eq_task:
            self._eq_task.cancel()
            self._eq_task = None
        if self.mc is not None:
            try:
                await self.mc.disconnect()
            except Exception:
                pass
            self._log("Nœud déconnecté")
        self.mc = None
        self.port = None
        self.mode = None
        self.radio = {}
        self.canaux = []
        self.busy = False
        self.next_cycle = None
        self.message = "Non connecté"
        self.store._bump()

    async def _cycle_loop(self):
        while self.mc is not None:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.busy = False
                self._log(f"Erreur pendant l'interrogation : {e}")
            while True:
                wait = self._attente()
                self.next_cycle = time.time() + wait
                self.store._bump()
                self._wake.clear()
                self._recalcul = False
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    break
                if not self._recalcul:  # « Interroger maintenant »
                    break

    # ---------- un cycle d'interrogation ----------
    async def _cycle(self):
        mc = self.mc
        while self.couverture_en_cours or self.prioritaire:
            await asyncio.sleep(2)
        self.busy = True
        self.message = "Lecture des contacts du nœud companion…"
        self.store._bump()

        res = await mc.commands.get_contacts()
        if res is None or res.type == EventType.ERROR:
            raise RuntimeError("le nœud companion ne renvoie pas sa liste de contacts")
        self._contacts = dict(res.payload or {})
        self._update_equipes()

        if self.store.get_config("decouverte", True):
            self._discover()

        targets = [n for n in self.store.list_nodes()
                   if (n.get("proprio") != "externe" or re.sub(r"[^a-z0-9]", "", (n.get("nom") or "").lower()) == "fr06mouginsf4jcf")
                   and n.get("statut") not in ("prevu", "supprime") and not n.get("eteint")]
        for i, node in enumerate(targets, 1):
            if self.mc is None:
                return
            if self.prioritaire or self.couverture_en_cours:
                # l'opérateur a demandé un test ou une commande : on lui laisse la place, la suite au prochain cycle
                self.busy = False
                self.message = "Interrogation suspendue au profit de la demande de l'opérateur"
                self.store._bump()
                return
            self.message = f"Interrogation {i}/{len(targets)} : {node.get('nom')}"
            self.store._bump()
            await self._poll_node(node)

        self.busy = False
        self.last_cycle = now_iso()
        self.message = f"Dernière interrogation terminée ({len(targets)} nœuds)"
        self.store._bump()

    def _contact_for(self, name: str):
        low = (name or "").strip().lower()
        for c in self._contacts.values():
            if (c.get("adv_name") or "").strip().lower() == low:
                return c
        return None

    def _name_for_prefix(self, prefix: str) -> str:
        prefix = (prefix or "").lower()
        for c in self._contacts.values():
            if c.get("public_key", "").lower().startswith(prefix):
                return c.get("adv_name") or prefix
        return f"? {prefix}"

    def _discover(self):
        """Ajoute ou complète les répéteurs entendus par le nœud companion."""
        added = 0
        for c in self._contacts.values():
            t = c.get("type")
            name = (c.get("adv_name") or "").strip()
            if t not in ROLE_BY_TYPE or not name:
                continue
            lat, lon = c.get("adv_lat") or 0, c.get("adv_lon") or 0
            has_pos = abs(lat) > 0.01 and abs(lon) > 0.01
            node = self.store.find_by_name(name)
            last_seen = None
            if c.get("last_advert"):
                last_seen = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(c["last_advert"]))
            if node is None and name in (self.store.get_config("ignores", []) or []):
                continue
            if node is None:
                self.store.save_node({
                    "nom": name, "proprio": "externe", "statut": "en-ligne", "role": ROLE_BY_TYPE[t],
                    "lat": round(lat, 5) if has_pos else "", "lon": round(lon, 5) if has_pos else "",
                    "voisins": [], "decouvert": True, "dernierAdvert": last_seen,
                    "cle": c.get("public_key", "")[:12], "cleComplete": c.get("public_key", ""),
                    **({"latAnnonce": round(lat, 5), "lonAnnonce": round(lon, 5)} if has_pos else {}),
                }, bump=False)
                added += 1
            else:
                fields = {"dernierAdvert": last_seen, "cle": c.get("public_key", "")[:12], "cleComplete": c.get("public_key", ""),
                          "chemin": self._chemin(c), "cheminDate": now_iso()}
                if has_pos:
                    fields.update(latAnnonce=round(lat, 5), lonAnnonce=round(lon, 5), posAnnonceDate=last_seen)
                    # la carte suit la position annoncée par le nœud, sauf si une position a été saisie à la main
                    # (ou si le GPS de sa télémétrie est connu, plus précis)
                    if (node.get("lat") in ("", None) or node.get("lon") in ("", None)
                            or (not node.get("posManuelle") and node.get("latGps") in ("", None))):
                        fields.update(lat=round(lat, 5), lon=round(lon, 5))
                node.update(fields)
                self.store.save_node(node, bump=False)
        if added:
            self._log(f"{added} nouveau(x) répéteur(s) découvert(s) via les annonces")
        self.store._bump()

    async def _poll_node(self, node: dict):
        mc = self.mc
        name = node.get("nom", "")
        contact = self._contact_for(name)
        if contact is None:
            self.store.patch_node(node["id"], {"dernierEssai": now_iso(),
                                               "lecture": "Absent des contacts du nœud companion (pas encore entendu)"})
            return

        pwd = self.store.password(node["id"])
        admin = False
        try:
            login = await mc.commands.send_login_sync(contact, pwd, min_timeout=8)
            admin = bool(login and login.type == EventType.LOGIN_SUCCESS and
                         (login.payload or {}).get("is_admin", bool(pwd)))
        except Exception:
            login = None

        status = await self._statut_robuste(contact)

        if not status:
            # pas d'état lu : le répéteur est-il quand même vivant ? (trace directe, annonce récente)
            preuve = await self._preuve_de_vie(contact)
            if preuve:
                self._marquer_vivant(node, f"Joignable ({preuve}) mais état non lu cette fois")
                return
            self.store.add_attempt(node["id"], False)
            await self._mark_failure(node)
            return
        self.store.add_attempt(node["id"], True)

        fields = {
            "bruit": status.get("noise_floor"),
            "erreursRx": status.get("recv_errors") if status.get("recv_errors") is not None else node.get("erreursRx", ""),
            "batterie": round(status["bat"] / 1000, 2) if status.get("bat") else node.get("batterie", ""),
            "uptime": status.get("uptime"),
            "dernierSnr": status.get("last_snr"),
            "paquetsRecus": status.get("nb_recv"),
            "paquetsEmis": status.get("nb_sent"),
            "maj": now_iso(), "dernierEssai": now_iso(), "echecs": 0,
            "lecture": "Lecture automatique OK" + ("" if admin else " (sans droits admin : voisins et firmware non lus)"),
        }
        self.store.add_measure(node["id"], status)
        fields["chemin"] = self._chemin(contact)
        try:
            lpp = await mc.commands.req_telemetry_sync(contact, min_timeout=8)
            if isinstance(lpp, list) and lpp:
                fields["telemetrie"] = [{"canal": x.get("channel"), "type": x.get("type"), "valeur": x.get("value")}
                                        for x in lpp if isinstance(x, dict)]
                fields["telemetrieDate"] = now_iso()
                for x in lpp:
                    v = x.get("value") if isinstance(x, dict) and x.get("type") == "gps" else None
                    if isinstance(v, dict) and v.get("latitude") is not None and v.get("longitude") is not None \
                            and (abs(float(v["latitude"])) > 0.01 or abs(float(v["longitude"])) > 0.01):
                        fields.update(latGps=round(float(v["latitude"]), 5), lonGps=round(float(v["longitude"]), 5), posGpsDate=now_iso())
                        if not node.get("posManuelle"):
                            fields.update(lat=fields["latGps"], lon=fields["lonGps"])
                        break
        except Exception as e:  # noqa: BLE001
            log.debug("télémétrie %s : %s", name, e)
        self._check_battery(node, fields.get("batterie"))

        if admin:
            try:
                nb = await mc.commands.fetch_all_neighbours(contact, min_timeout=10)
                if nb and isinstance(nb.get("neighbours"), list):
                    fields["voisins"] = [
                        {"nom": self._name_for_prefix(v.get("pubkey", "")), "snr": v.get("snr"),
                         "ilYa": v.get("secs_ago")}
                        for v in nb["neighbours"]
                    ]
            except Exception as e:  # noqa: BLE001
                log.debug("voisins %s : %s", name, e)
            ver = await self._cli(contact, "ver")
            if ver:
                m = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", ver)
                if m:
                    fields["firmware"] = "v" + m.group(1)
                    if node.get("otaDebut"):
                        self._confirmer_ota(node, fields["firmware"], "AUTO")
                        fields["otaDebut"] = ""
            h = self._horloge(await self._cli(contact, "clock"))
            if h and abs(h.get("horlogeEcart", 0)) >= 120 and self.store.get_config("horloge_auto", True):
                ecart = h["horlogeEcart"]
                try:
                    await mc.commands.set_time(int(time.time()))
                except Exception:  # noqa: BLE001
                    pass
                rep_sync = await self._cli(contact, "clock sync")
                h2 = self._horloge(await self._cli(contact, "clock"))
                bon = bool(h2) and abs(h2.get("horlogeEcart", 999)) < 120
                sens = "en avance" if ecart > 0 else "en retard"
                texte = (f"Heure remise automatiquement ({sens} de {abs(ecart) // 60} min)"
                         + (" : corrigée." if bon else " : la correction n'a pas pu être vérifiée."))
                self.store.add_journal(name, "AUTO", texte, auto=True)
                fields["derniereRequete"] = {"libelle": "Remise à l'heure automatique", "date": now_iso(), "ok": bon,
                                             "resultat": (rep_sync or "").strip()[:160] or ("corrigée" if bon else "non vérifiée"),
                                             "operateur": "AUTO"}
                h = h2 or h
            fields.update(h)
            tx = await self._cli(contact, "get tx")
            if tx:
                m = re.search(r"(-?\d+)", tx)
                if m:
                    fields["tx"] = int(m.group(1))

        old = node.get("statut")
        if old in ("degrade", "hors-ligne"):
            fields["statut"] = "en-ligne"
            self.store.add_journal(name, "AUTO", f"{name} répond de nouveau (était {old}).", auto=True)
            if old == "hors-ligne":
                self.alerte("info", name, f"{name} répond de nouveau.")
        try:
            fields.update(self._analyse(node, fields))
        except Exception as e:  # noqa: BLE001
            log.warning("analyse des tendances %s : %s", name, e)
        self.store.patch_node(node["id"], fields)

    async def _statut_robuste(self, contact):
        """Demande d'état avec 2e essai, puis en inondation si la route mémorisée ne passe plus."""
        mc = self.mc
        for essai in range(3):
            if essai == 2:
                n = contact.get("out_path_len")
                if n is None or n < 0:
                    break
                try:  # route mémorisée périmée : on repart en inondation
                    await mc.commands.reset_path(contact)
                    contact["out_path_len"], contact["out_path"] = -1, ""
                except Exception:  # noqa: BLE001
                    break
            try:
                st = await mc.commands.req_status_sync(contact, min_timeout=10 + 5 * essai)
            except Exception:  # noqa: BLE001
                st = None
            if st:
                return st
            await asyncio.sleep(2)
        return None

    async def _preuve_de_vie(self, contact) -> str | None:
        adv = contact.get("last_advert") or 0
        if adv and time.time() - adv < 3 * 3600:
            return "annonce reçue il y a " + str(int((time.time() - adv) // 60)) + " min"
        try:
            if await self._trace(contact):
                return "voisin direct, trace OK"
        except Exception:  # noqa: BLE001
            pass
        return None

    def _marquer_vivant(self, node: dict, lecture: str | None = None, extra: dict | None = None):
        """Toute réponse d'un répéteur prouve qu'il est en service : état « en ligne », compteur d'échecs remis à zéro."""
        node = self.store.get_node(node["id"]) or node
        name = node.get("nom", "")
        fields = {"echecs": 0, "dernierEssai": now_iso(), "maj": now_iso(), **(extra or {})}
        if lecture:
            fields["lecture"] = lecture
        self.store.add_attempt(node["id"], True)
        old = node.get("statut")
        if old in ("degrade", "hors-ligne"):
            fields["statut"] = "en-ligne"
            self.store.add_journal(name, "AUTO", f"{name} répond de nouveau (était {old}).", auto=True)
            if old == "hors-ligne":
                self.alerte("info", name, f"{name} répond de nouveau.")
        self.store.patch_node(node["id"], fields)

    async def _cli(self, contact, cmd: str, timeout: float = 20) -> str | None:
        """Envoie une commande console à un répéteur (droits admin requis) et attend sa réponse."""
        mc = self.mc
        prefix = contact.get("public_key", "")[:12]
        try:
            waiter = asyncio.ensure_future(mc.dispatcher.wait_for_event(
                EventType.CONTACT_MSG_RECV, attribute_filters={"pubkey_prefix": prefix}, timeout=timeout))
            await asyncio.sleep(0)
            sent = await mc.commands.send_cmd(contact, cmd)
            if sent is None or sent.type == EventType.ERROR:
                waiter.cancel()
                return None
            ev = await waiter
            return (ev.payload or {}).get("text") if ev else None
        except Exception:
            return None

    async def _mark_failure(self, node: dict):
        fails = int(node.get("echecs") or 0) + 1
        fields = {"echecs": fails, "dernierEssai": now_iso(), "lecture": f"Pas de réponse ({fails} essai(s) consécutif(s))"}
        old = node.get("statut")
        if old != "test":
            new = "hors-ligne" if fails >= FAILS_OFFLINE else ("degrade" if fails >= FAILS_DEGRADE else old)
            if new != old:
                fields["statut"] = new
                label = {"hors-ligne": "hors ligne", "degrade": "dégradé"}[new]
                self.store.add_journal(node.get("nom", ""), "AUTO",
                                       f"{node.get('nom')} ne répond pas ({fails} essai(s)) : passé en {label}.", auto=True)
                if new == "hors-ligne":
                    self.alerte("critique", node.get("nom", ""), f"{node.get('nom')} est hors ligne ({fails} essais sans réponse).")
        self.store.patch_node(node["id"], fields)


    # ---------- chemins ----------
    def _chemin(self, contact: dict) -> dict:
        """Chemin connu par le nœud companion pour joindre ce contact : direct, relais, ou inondation."""
        n = contact.get("out_path_len", -1)
        if n is None or n < 0:
            return {"type": "inondation", "sauts": None, "relais": []}
        if n == 0:
            return {"type": "direct", "sauts": 0, "relais": []}
        size = max(1, (contact.get("out_path_hash_mode") or 0) + 1)
        hexpath = contact.get("out_path") or ""
        hops = [hexpath[i:i + 2 * size] for i in range(0, len(hexpath), 2 * size)][:n]
        relais = []
        for h in hops:
            noms = [c.get("adv_name") for c in self._contacts.values()
                    if c.get("type") in (2, 3) and c.get("public_key", "").lower().startswith(h.lower())]
            relais.append(noms[0] if len(noms) == 1 else (f"{noms[0]} (?)" if noms else f"? {h}"))
        return {"type": "relais", "sauts": n, "relais": relais}

    # ---------- alertes batterie ----------
    def _check_battery(self, node: dict, volts):
        try:
            v = float(volts)
        except (TypeError, ValueError):
            return
        if v <= 0:
            return
        seuil = float(self.store.get_config("seuil_batterie", 3.5) or 3.5)
        flagged = bool(node.get("alerteBatterie"))
        if v < seuil and not flagged:
            self.alerte("attention", node.get("nom", ""), f"Batterie faible sur {node.get('nom')} : {v:.2f} V (seuil {seuil:.2f} V).")
            self.store.add_journal(node.get("nom", ""), "AUTO", f"Batterie faible : {v:.2f} V.", auto=True)
            self.store.patch_node(node["id"], {"alerteBatterie": True})
        elif v >= seuil + 0.1 and flagged:
            self.store.patch_node(node["id"], {"alerteBatterie": False})

    # ---------- messagerie ----------
    async def _load_channels(self):
        out = []
        cle_public = "8b3387e9c5cdea6ac9e5edbaa115cd72"
        for i in range(40):
            try:
                ev = await self.mc.commands.get_channel(i)
            except Exception:
                break
            if ev is None or ev.type == EventType.ERROR:
                break
            p = ev.payload or {}
            name = (p.get("channel_name") or "").strip("\x00 ")
            if name:
                secret = p.get("channel_secret") or b""
                secret = secret.hex() if isinstance(secret, (bytes, bytearray)) else str(secret)
                nh = name if name.startswith("#") else "#" + name
                if secret == cle_public:
                    genre = "public"
                elif secret and secret == hashlib.sha256(nh.encode("utf-8")).hexdigest()[:32]:
                    genre = "hashtag"
                else:
                    genre = "prive"
                out.append({"index": i, "nom": name, "genre": genre})
                self._secrets[i] = secret
        self.canaux = out or [{"index": 0, "nom": "Public"}]
        # canal ADRASEC 06 : périmètre de région « fr-06 » (réglage de l'application MeshCore), une seule fois
        if not self.store.get_config("region_adrasec_init", False):
            ch = next((c for c in out if re.sub(r"[^a-z0-9]", "", c["nom"].lower()) == "adrasec06"), None)
            if ch is not None:
                rc = dict(self.store.get_config("regions_canaux", {}) or {})
                rc.setdefault(str(ch["index"]), "#fr-06")
                self.store.set_config("regions_canaux", rc)
                self.store.set_config("region_adrasec_init", True)
        self.store._bump()

    def partage_canal(self, idx) -> dict:
        """Lien et QR code « meshcore://channel/add » du canal, comme l'application MeshCore."""
        from urllib.parse import quote
        idx = int(idx)
        ch = next((c for c in self.canaux if c["index"] == idx), None)
        secret = self._secrets.get(idx, "")
        if ch is None or not secret:
            return {"ok": False, "erreur": "Clé du canal inconnue : connectez le nœud companion."}
        region = (self.store.get_config("regions_canaux", {}) or {}).get(str(idx), "")
        lien = f"meshcore://channel/add?name={quote(ch['nom'])}&secret={secret}"
        if region and region != "*":
            lien += f"&region_scope={quote(region.lstrip('#'))}"
        svg = ""
        try:
            import io
            import segno
            buf = io.BytesIO()
            segno.make(lien, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff", xmldecl=False)
            svg = buf.getvalue().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("QR code du canal : %s", e)
        return {"ok": True, "nom": ch["nom"], "genre": ch.get("genre"), "secret": secret,
                "ancienne": bool((self.store.get_config("anciennes_cles_canaux", {}) or {}).get(str(idx))),
                "region": region.lstrip("#") if region and region != "*" else "", "lien": lien, "qr": svg}

    def modifier_cle_canal(self, idx, secret: str = "", ancienne: bool = False, operateur: str = "PC") -> dict:
        """Change la clé secrète d'un canal dans le nœud companion (clé saisie, aléatoire, ou retour à l'ancienne)."""
        import secrets as _secrets
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        idx = int(idx)
        ch = next((c for c in self.canaux if c["index"] == idx), None)
        if ch is None:
            return {"ok": False, "erreur": "Canal inconnu."}
        anciennes = dict(self.store.get_config("anciennes_cles_canaux", {}) or {})
        if ancienne:
            secret = anciennes.get(str(idx), "")
            if not secret:
                return {"ok": False, "erreur": "Aucune ancienne clé mémorisée pour ce canal."}
        secret = re.sub(r"\s", "", secret or "").lower() or _secrets.token_hex(16)
        if not re.fullmatch(r"[0-9a-f]{32}", secret):
            return {"ok": False, "erreur": "La clé doit faire 32 caractères hexadécimaux (0-9, a-f)."}
        avant = self._secrets.get(idx, "")
        if secret == avant:
            return {"ok": False, "erreur": "C'est déjà la clé de ce canal."}

        async def go():
            # trame « set channel » envoyée directement : la bibliothèque recalculerait la clé d'un nom commençant par #
            nom = ch["nom"].encode("utf-8")[:32].ljust(32, b"\x00")
            ev = await self.mc.commands.send(b"\x20" + idx.to_bytes(1, "little") + nom + bytes.fromhex(secret),
                                             [EventType.OK, EventType.ERROR])
            if ev is None or ev.type == EventType.ERROR:
                return False
            await self._load_channels()
            return True
        try:
            ok = self._submit(go(), timeout=30)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Modification impossible : {e}"}
        if not ok:
            return {"ok": False, "erreur": "Le nœud companion a refusé la nouvelle clé."}
        if avant:
            anciennes[str(idx)] = avant
            self.store.set_config("anciennes_cles_canaux", anciennes)
        self.store.add_journal("", operateur, f"Clé secrète du canal {ch['nom']} changée" +
                               (" (retour à l'ancienne clé)" if ancienne else "") +
                               " : les membres doivent scanner le nouveau QR code.", nature="evenement")
        self._log(f"Clé du canal {ch['nom']} changée")
        return {"ok": True, **self.partage_canal(idx)}

    def liste_contacts(self) -> dict:
        """Tous les contacts du nœud companion (répéteurs, compagnons, salons, capteurs) pour la liste triable."""
        if not self._contacts and self.mc is not None:
            async def lire():
                res = await self.mc.commands.get_contacts()
                if res is not None and res.type != EventType.ERROR:
                    self._contacts = dict(res.payload or {})
            try:
                self._submit(lire(), timeout=60)
            except Exception:  # noqa: BLE001
                pass
        amis = set(self.store.get_config("amis", []) or [])
        types = {1: "compagnon", 2: "repeteur", 3: "salon", 4: "capteur"}
        out = []
        for c in self._contacts.values():
            cle = c.get("public_key") or ""
            la, lo = c.get("adv_lat"), c.get("adv_lon")
            try:
                pos = abs(float(la)) > 0.01 or abs(float(lo)) > 0.01
            except (TypeError, ValueError):
                pos = False
            out.append({"cle": cle, "nom": c.get("adv_name") or cle[:12], "type": types.get(c.get("type"), "autre"),
                        "annonce": c.get("last_advert") or 0, "modif": c.get("lastmod") or 0,
                        "lat": float(la) if pos else None, "lon": float(lo) if pos else None,
                        "sauts": c.get("out_path_len"), "ami": cle in amis})
        return {"ok": True, "contacts": out}

    def contacts_messagerie(self) -> list[dict]:
        amis = set(self.store.get_config("amis", []) or [])
        out = [{"cle": c.get("public_key", ""), "nom": c.get("adv_name", ""), "ami": c.get("public_key", "") in amis,
                "annonce": c.get("last_advert") or 0}
               for c in self._contacts.values() if c.get("type") == 1 and c.get("adv_name")]
        out.sort(key=lambda x: (not x["ami"], x["nom"].lower()))
        return out

    # ---------- amis (companions suivis) ----------
    @staticmethod
    def _norm(s: str) -> str:
        """Nom comparable : sans emoji, espaces, tirets ni majuscules (« F4EUB ☢️ QRA » = « f4eubqra »)."""
        return re.sub(r"[^0-9a-z]", "", (s or "").casefold())

    def _chercher_annonce(self, nom: str) -> dict | None:
        """Dernière annonce entendue d'un companion, nom comparé sans emoji ni majuscules (puis début de nom)."""
        n = self._norm(nom)
        if not n:
            return None
        ann = [a for a in self.store.annonces_liste() if int(a.get("type") or 1) == 1]
        exact = [a for a in ann if self._norm(a.get("nom")) == n]
        if exact:
            return exact[0]
        proches = [a for a in ann if len(n) >= 4 and (self._norm(a.get("nom")).startswith(n) or n.startswith(self._norm(a.get("nom")) or "~"))]
        return proches[0] if len(proches) == 1 else None

    def companions_connus(self, q: str = "") -> dict:
        """Companions dont la clé complète est connue : contacts du nœud et annonces entendues, filtrés par nom."""
        n = self._norm(q)
        vus, out = set(), []
        for c in self._contacts.values():
            if c.get("type") != 1 or not c.get("adv_name"):
                continue
            k = (c.get("public_key") or "").lower()
            if n and n not in self._norm(c["adv_name"]) and not k.startswith(q.strip().lower() or "~"):
                continue
            vus.add(k)
            out.append({"nom": c["adv_name"], "cle": k, "source": "contact", "ts": c.get("last_advert") or 0})
        for a in self.store.annonces_liste():
            k = (a.get("cle") or "").lower()
            if int(a.get("type") or 1) != 1 or k in vus or not a.get("nom"):
                continue
            if n and n not in self._norm(a["nom"]) and not k.startswith(q.strip().lower() or "~"):
                continue
            vus.add(k)
            out.append({"nom": a["nom"], "cle": k, "source": "annonce", "ts": a.get("ts") or 0})
        out.sort(key=lambda x: -(x["ts"] or 0))
        return {"ok": True, "companions": out[:40], "attente": self.store.get_config("amis_attente", []) or []}

    def attendre_ami(self, nom: str, retirer: bool = False) -> dict:
        """Ami « en attente » : il sera ajouté aux contacts et aux amis dès que son annonce sera entendue."""
        nom = (nom or "").strip()
        if not self._norm(nom):
            return {"ok": False, "erreur": "Indiquez le nom du companion."}
        att = [x for x in (self.store.get_config("amis_attente", []) or []) if self._norm(x.get("nom")) != self._norm(nom)]
        if not retirer:
            att.append({"nom": nom, "date": now_iso()})
        self.store.set_config("amis_attente", att)
        self.store._bump()
        return {"ok": True, "attente": att,
                "message": f"{nom} retiré de l'attente" if retirer else
                f"{nom} sera ajouté automatiquement à vos amis dès que son annonce sera entendue"}

    async def _ami_entendu(self, a: dict):
        try:
            r = await self._ajouter_annonce(a, True)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "erreur": str(e)}
        if r.get("ok"):
            att = [x for x in (self.store.get_config("amis_attente", []) or []) if self._norm(x.get("nom")) != self._norm(a.get("nom"))]
            self.store.set_config("amis_attente", att)
            self.alerte("info", a.get("nom", ""), f"Annonce de {a.get('nom')} entendue : ajouté à vos contacts et à vos amis ★.")
            self._log(f"Ami en attente {a.get('nom')} ajouté (clé {a['cle'][:12]}…)")
        else:
            log.warning("Ajout automatique de l'ami %s : %s", a.get("nom"), r.get("erreur"))
    def basculer_ami(self, cle: str, ami=None) -> dict:
        c = next((c for c in self._contacts.values() if (c.get("public_key") or "").startswith((cle or "~").lower())), None)
        full = c.get("public_key") if c else (cle or "").lower()
        amis = list(self.store.get_config("amis", []) or [])
        etat = (full not in amis) if ami is None else bool(ami)
        amis = [a for a in amis if a != full] + ([full] if etat else [])
        self.store.set_config("amis", amis)
        self._update_equipes()
        self.store._bump()
        return {"ok": True, "ami": etat, "nom": c.get("adv_name") if c else full[:12]}

    def ajouter_contact(self, texte: str, nom: str = "", ami: bool = True) -> dict:
        """Ajoute un companion au nœud : carte de contact « meshcore://… » (partagée depuis l'application ou la carte)
        ou clé publique de 64 caractères hexadécimaux + nom."""
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        try:
            return self._submit(self._ajouter_contact(texte or "", (nom or "").strip(), ami), timeout=60)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Ajout impossible : {e}"}

    def ajouter_depuis_nom(self, nom: str, ami: bool = False) -> dict:
        """Ajoute aux contacts un nœud vu sur un canal, grâce à sa dernière annonce entendue (clé publique complète)."""
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        nom = (nom or "").strip()
        c = next((c for c in self._contacts.values() if self._norm(c.get("adv_name")) == self._norm(nom)), None)
        if c:
            if ami:
                self.basculer_ami(c["public_key"], True)
            return {"ok": True, "deja": True, "cle": c["public_key"], "message": f"{nom} est déjà dans vos contacts" + (" : ajouté aux amis ★." if ami else ".")}
        a = self.store.annonce_par_nom(nom) or self._chercher_annonce(nom)
        if not a:
            return {"ok": False, "inconnu": True,
                    "erreur": f"Aucune annonce de {nom} n'a encore été entendue : sa clé publique est inconnue. "
                              "Demandez-lui d'envoyer une annonce (advert), ou collez sa carte de contact."}
        try:
            return self._submit(self._ajouter_annonce(a, ami), timeout=60)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Ajout impossible : {e}"}

    async def _ajouter_annonce(self, a: dict, ami: bool) -> dict:
        contact = {"public_key": a["cle"], "type": int(a.get("type") or 1), "flags": 0, "out_path_len": -1, "out_path": "",
                   "out_path_hash_mode": 0, "adv_name": (a.get("nom") or "")[:31], "last_advert": int(a.get("ts") or time.time()),
                   "adv_lat": float(a.get("lat") or 0.0), "adv_lon": float(a.get("lon") or 0.0)}
        ev = await self.mc.commands.add_contact(contact)
        if ev is None or ev.type == EventType.ERROR:
            return {"ok": False, "erreur": "Le nœud companion a refusé le contact (mémoire pleine ?)."}
        res = await self.mc.commands.get_contacts()
        if res is not None and res.type != EventType.ERROR:
            self._contacts = dict(res.payload or {})
        if ami:
            self.basculer_ami(a["cle"], True)
        self._update_equipes()
        self.store.add_journal(a.get("nom", ""), self.store.get_config("operateur", "") or "PC",
                               f"{a.get('nom')} ajouté aux contacts depuis son annonce" + (" (ami)." if ami else "."))
        self.store._bump()
        return {"ok": True, "cle": a["cle"], "message": f"{a.get('nom')} ajouté à vos contacts" + (" et à vos amis ★" if ami else "")}

    async def _ajouter_contact(self, texte: str, nom: str, ami: bool) -> dict:
        texte = texte.strip()
        m_add = re.search(r"meshcore://contact/add\?(\S+)", texte)
        if m_add:
            from urllib.parse import parse_qs
            q = parse_qs(m_add.group(1))
            cle_q = (q.get("public_key") or [""])[0]
            if re.fullmatch(r"[0-9a-fA-F]{64}", cle_q):
                nom = nom or (q.get("name") or [""])[0].strip()
                texte = cle_q
        m_uri = re.search(r"meshcore://([0-9a-fA-F]{100,})", texte)
        avant = set(self._contacts)
        cle = None
        if m_uri:
            ev = await self.mc.commands.import_contact(bytes.fromhex(m_uri.group(1)))
            if ev is None or ev.type == EventType.ERROR:
                return {"ok": False, "erreur": "Le nœud companion a refusé cette carte de contact (lien incomplet ou abîmé)."}
        else:
            m_cle = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])", texte)
            if not m_cle and not texte and nom:
                # pas de clé : on la cherche dans les contacts et les annonces entendues
                c = next((c for c in self._contacts.values() if c.get("type") == 1 and self._norm(c.get("adv_name")) == self._norm(nom)), None)
                if c:
                    if ami:
                        self.basculer_ami(c["public_key"], True)
                    return {"ok": True, "cle": c["public_key"], "message": f"{c.get('adv_name')} est déjà dans vos contacts" + (" : ajouté aux amis ★." if ami else ".")}
                a = self._chercher_annonce(nom)
                if a:
                    return await self._ajouter_annonce(a, ami)
                return {"ok": False, "inconnu": True,
                        "erreur": f"Aucune annonce de « {nom} » n'a encore été entendue : sa clé publique est inconnue."}
            if not m_cle:
                return {"ok": False, "erreur": "Collez une carte de contact « meshcore://… » ou la clé publique complète (64 caractères hexadécimaux)."}
            cle = m_cle.group(1).lower()
            if not nom:
                m_nom = re.search(r"(?:name|nom)=([^&\s]+)", texte)
                nom = re.sub(r"%20|\+", " ", m_nom.group(1)) if m_nom else ""
            if not nom:
                return {"ok": False, "erreur": "Indiquez le nom du companion (ex. F4jrc-c)."}
            if any((c.get("public_key") or "").lower() == cle for c in self._contacts.values()):
                if ami:
                    self.basculer_ami(cle, True)
                return {"ok": True, "message": f"{nom} est déjà dans vos contacts" + (" : ajouté aux amis." if ami else "."), "cle": cle}
            contact = {"public_key": cle, "type": 1, "flags": 0, "out_path_len": -1, "out_path": "", "out_path_hash_mode": 0,
                       "adv_name": nom[:31], "last_advert": int(time.time()), "adv_lat": 0.0, "adv_lon": 0.0}
            ev = await self.mc.commands.add_contact(contact)
            if ev is None or ev.type == EventType.ERROR:
                return {"ok": False, "erreur": "Le nœud companion a refusé le contact (mémoire pleine ?)."}
        res = await self.mc.commands.get_contacts()
        if res is not None and res.type != EventType.ERROR:
            self._contacts = dict(res.payload or {})
        nouveaux = [self._contacts[k] for k in self._contacts if k not in avant]
        c = None
        if cle:
            c = next((c for c in self._contacts.values() if (c.get("public_key") or "").lower() == cle), None)
        elif nouveaux:
            c = nouveaux[0]
        elif m_uri:
            c = next((c for c in self._contacts.values() if (c.get("public_key") or "").lower() in m_uri.group(1).lower()), None)
        if c is None:
            return {"ok": False, "erreur": "Le contact n'apparaît pas dans le nœud companion après l'ajout."}
        if ami:
            self.basculer_ami(c["public_key"], True)
        self._update_equipes()
        self.store.add_journal(c.get("adv_name", ""), self.store.get_config("operateur", "") or "PC",
                               f"Companion {c.get('adv_name')} ajouté aux contacts" + (" (ami)." if ami else "."))
        self.store._bump()
        return {"ok": True, "message": f"{c.get('adv_name')} ajouté à vos contacts" + (" et à vos amis ★" if ami else ""),
                "cle": c.get("public_key")}

    def _canal_nom(self, idx) -> str:
        for c in self.canaux:
            if c["index"] == idx:
                return c["nom"]
        return f"Canal {idx}"

    async def _on_channel_msg(self, event):
        p = event.payload or {}
        if p.get("txt_type") not in (0, None):
            return
        texte = p.get("text", "")
        auteur, _, corps = texte.partition(": ")
        if not corps:
            auteur, corps = "", texte
        h = p.get("txt_hash")
        if h is None:
            try:
                ts = int(p.get("sender_timestamp") or 0).to_bytes(4, "little", signed=False)
                h = int.from_bytes(hashlib.sha256(ts + texte.encode("utf-8")).digest()[:4], "little", signed=False)
            except Exception:
                h = None
        self.store.add_message({"sens": "recu", "canal": p.get("channel_idx"), "canal_nom": self._canal_nom(p.get("channel_idx")),
                                "auteur": auteur, "texte": corps, "snr": p.get("SNR"), "rssi": p.get("RSSI"),
                                "msg_hash": h, "chemin": p.get("path"),
                                "sauts": p.get("path_len") if p.get("path_len") not in (255, None) else None})

    async def _on_rx_log(self, event):
        """Chaque paquet de canal entendu par le nœud companion : on garde son parcours (relais traversés)."""
        p = event.payload or {}
        try:
            if p.get("payload_type") == 0x04 and p.get("adv_key") and p.get("adv_name"):
                lat, lon = p.get("adv_lat"), p.get("adv_lon")
                ok = lat is not None and lon is not None and abs(lat) > .01
                self.store.annonce_vue(p["adv_key"], p["adv_name"], int(p.get("adv_type") or 1),
                                       lat if ok else None, lon if ok else None)
                att = self.store.get_config("amis_attente", []) or []
                if att and int(p.get("adv_type") or 1) == 1 and self.mc is not None \
                        and any(self._norm(x.get("nom")) == self._norm(p["adv_name"]) for x in att) \
                        and not any((c.get("public_key") or "").lower() == p["adv_key"].lower() for c in self._contacts.values()):
                    asyncio.ensure_future(self._ami_entendu({"cle": p["adv_key"].lower(), "nom": p["adv_name"], "type": 1,
                                                             "lat": lat if ok else 0.0, "lon": lon if ok else 0.0, "ts": time.time()}))
                return
            if p.get("payload_type") != 0x05 or not p.get("message"):
                return
            self.store.add_reception({"msg_hash": p.get("msg_hash"), "ts": p.get("recv_time") or time.time(),
                                      "texte": p.get("message"), "chemin": p.get("path") or "",
                                      "taille": p.get("path_hash_size") or 1, "snr": p.get("snr"), "rssi": p.get("rssi")})
            if self.local_name and str(p.get("message")).startswith(self.local_name + ": "):
                self.store._bump()  # écho d'un de nos messages : mise à jour du compteur de répéteurs
        except Exception as e:  # noqa: BLE001
            log.debug("Réception ignorée : %s", e)

    # ---------- détail d'un message ----------
    def detail_message(self, mid) -> dict:
        m = self.store.get_message(int(mid))
        if not m:
            return {"ok": False, "erreur": "Message introuvable."}
        moi_nom = self.local_name or "Ce nœud"
        moi = {"nom": moi_nom, "lat": None, "lon": None}
        if self.position:
            moi.update(lat=self.position.get("lat"), lon=self.position.get("lon"))
        # répertoire des relais connus : nœuds de la base + contacts du nœud companion
        repert = []
        for n in self.store.list_nodes():
            if n.get("cle") and n.get("statut") != "supprime":
                repert.append({"cle": n["cle"].lower(), "nom": n.get("nom"), "lat": n.get("lat"), "lon": n.get("lon"),
                               "adrasec": n.get("proprio") != "externe"})
        connus = {r["cle"][:12] for r in repert}
        for c in self._contacts.values():
            k = (c.get("public_key") or "").lower()
            if c.get("type") in (2, 3) and k and k[:12] not in connus:
                lat, lon = c.get("adv_lat") or None, c.get("adv_lon") or None
                repert.append({"cle": k, "nom": c.get("adv_name"), "lat": lat, "lon": lon, "adrasec": False})

        def pos(x):
            return x.get("lat") is not None and x.get("lon") is not None and abs(x["lat"]) > 0.01

        def dist(a, b):
            return math.hypot((a["lat"] - b["lat"]) * 111, (a["lon"] - b["lon"]) * 111 * math.cos(math.radians(a["lat"])))

        def resoudre(hexpath: str, taille: int) -> list[dict]:
            taille = max(1, int(taille or 1))
            hops = [hexpath[i:i + 2 * taille] for i in range(0, len(hexpath or ""), 2 * taille)]
            out = [None] * len(hops)
            suivant = moi if pos(moi) else None  # on remonte depuis le récepteur : chaque relais est à portée du suivant
            for i in range(len(hops) - 1, -1, -1):
                h = hops[i].lower()
                cands = [r for r in repert if r["cle"].startswith(h)]
                choix = None
                if len(cands) > 1 and suivant is not None:
                    avec = [c for c in cands if pos(c)]
                    if avec:
                        choix = min(avec, key=lambda c: dist(c, suivant))
                elif cands:
                    choix = cands[0]
                if choix is None and cands:
                    choix = cands[0]
                e = {"hash": h.upper(), "nom": choix["nom"] if choix else None, "adrasec": bool(choix and choix["adrasec"]),
                     "lat": choix["lat"] if choix and pos(choix) else None, "lon": choix["lon"] if choix and pos(choix) else None,
                     "candidats": len(cands)}
                out[i] = e
                if e["lat"] is not None:
                    suivant = e
            return out

        # auteur
        auteur_nom = m.get("auteur") or m.get("contact") or ""
        auteur = {"nom": auteur_nom, "lat": None, "lon": None, "source": None}
        if m.get("sens") == "emis":
            auteur.update(moi, source="ce nœud")
        else:
            c = next((c for c in self._contacts.values() if (c.get("adv_name") or "").lower() == auteur_nom.lower()), None)
            if c and abs(c.get("adv_lat") or 0) > 0.01:
                auteur.update(lat=c["adv_lat"], lon=c["adv_lon"], source="position annoncée")
            else:
                pp = self.store.last_position_by_name(auteur_nom)
                if pp:
                    auteur.update(lat=pp["lat"], lon=pp["lon"], source="dernière position connue")
                else:
                    n = next((n for n in repert if (n.get("nom") or "").lower() == auteur_nom.lower() and pos(n)), None)
                    if n:
                        auteur.update(lat=n["lat"], lon=n["lon"], source="position du nœud")

        routes = []
        if m.get("canal") is not None:
            plein = f"{m.get('auteur')}: {m.get('texte')}" if m.get("auteur") else m.get("texte")
            vus = set()
            for r in self.store.receptions_for(m.get("msg_hash"), plein, float(m.get("ts") or 0)):
                if (r["chemin"] or "") in vus:  # même parcours entendu deux fois
                    continue
                vus.add(r["chemin"] or "")
                routes.append({"ts": r["ts"], "snr": r["snr"], "rssi": r["rssi"], "relais": resoudre(r["chemin"], r["taille"])})
            if not routes and m.get("chemin") is not None:
                routes.append({"ts": m.get("ts"), "snr": m.get("snr"), "rssi": m.get("rssi"), "relais": resoudre(m["chemin"], 1)})
        route_connue = None
        if m.get("canal") is None and m.get("contact_cle"):
            c = next((c for c in self._contacts.values() if (c.get("public_key") or "").startswith(m["contact_cle"])), None)
            if c:
                n = c.get("out_path_len", -1)
                if n is not None and n >= 0:
                    route_connue = {"sauts": n, "relais": resoudre(c.get("out_path") or "", (c.get("out_path_hash_mode") or 0) + 1)}
        return {"ok": True, "message": m, "auteur": auteur, "moi": moi, "routes": routes, "route_connue": route_connue,
                "decodage": bool(self.mc)}

    async def _on_contact_msg(self, event):
        p = event.payload or {}
        if p.get("txt_type", 0) != 0:  # les réponses aux commandes console sont ignorées ici
            return
        prefix = p.get("pubkey_prefix", "")
        nom = next((c.get("adv_name") for c in self._contacts.values()
                    if c.get("public_key", "").startswith(prefix)), prefix)
        self.store.add_message({"sens": "recu", "contact": nom, "contact_cle": prefix, "auteur": nom,
                                "texte": p.get("text", ""), "snr": p.get("SNR"),
                                "sauts": p.get("path_len") if p.get("path_len") not in (255, None) else None})
        self.alerte("message", nom, f"Message de {nom} : {p.get('text', '')[:120]}")

    def envoyer_message(self, texte: str, canal=None, contact_cle=None) -> dict:
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        texte = (texte or "").strip()
        if not texte:
            return {"ok": False, "erreur": "Le message est vide."}
        if len(texte.encode("utf-8")) > 150:
            return {"ok": False, "erreur": "Message trop long (150 octets maximum)."}
        try:
            return self._submit(self._envoyer(texte, canal, contact_cle), timeout=30)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Envoi impossible : {e}"}

    async def _envoyer(self, texte, canal, contact_cle):
        moi = self.local_name or "moi"
        if contact_cle:
            c = next((c for c in self._contacts.values() if c.get("public_key") == contact_cle), None)
            if c is None:
                return {"ok": False, "erreur": "Contact introuvable dans le nœud companion."}
            mid = self.store.add_message({"sens": "emis", "contact": c.get("adv_name"), "contact_cle": contact_cle[:12],
                                          "auteur": moi, "texte": texte, "statut": "envoi…"})
            asyncio.ensure_future(self._suivre_accuse(mid, c, texte))
            ok = True
        else:
            idx = int(canal or 0)
            region = (self.store.get_config("regions_canaux", {}) or {}).get(str(idx), "")
            if region:
                try:
                    if region == "*":
                        await self.mc.commands.force_unscoped()
                    else:
                        await self.mc.commands.set_flood_scope(region)
                except Exception as e:  # noqa: BLE001
                    log.warning("région %s : %s", region, e)
            ev = await self.mc.commands.send_chan_msg(idx, texte)
            if region:
                try:
                    await self.mc.commands.set_flood_scope(None)  # retour à la région par défaut du nœud
                except Exception:  # noqa: BLE001
                    pass
            ok = ev is not None and ev.type != EventType.ERROR
            self.store.add_message({"sens": "emis", "canal": idx, "canal_nom": self._canal_nom(idx), "auteur": moi,
                                    "texte": texte, "statut": "envoyé" if ok else "échec"})
        return {"ok": ok} if ok else {"ok": False, "erreur": "Le nœud companion a refusé l'envoi."}

    async def _suivre_accuse(self, mid: int, contact: dict, texte: str):
        """Envoi avec jusqu'à 3 essais (les derniers en inondation) et attente de l'accusé de réception."""
        try:
            ev = await self.mc.commands.send_msg_with_retry(contact, texte, max_attempts=3, flood_after=2,
                                                            max_flood_attempts=2, min_timeout=8)
            self.store.set_message_status(mid, "reçu ✓" if ev is not None else "non confirmé ✗")
        except Exception as e:  # noqa: BLE001
            log.warning("accusé de réception : %s", e)
            self.store.set_message_status(mid, "non confirmé ✗")

    def renvoyer_message(self, mid: int) -> dict:
        m = self.store.get_message(int(mid))
        if not m or m.get("sens") != "emis":
            return {"ok": False, "erreur": "Message introuvable."}
        if m.get("canal") is not None:
            return self.envoyer_message(m["texte"], canal=m["canal"])
        c = next((c for c in self._contacts.values()
                  if (c.get("public_key") or "").startswith(m.get("contact_cle") or "~")), None)
        if c is None:
            return {"ok": False, "erreur": "Contact introuvable dans le nœud companion."}
        self.store.delete_message(int(mid))
        return self.envoyer_message(m["texte"], contact_cle=c["public_key"])

    # ---------- commandes à distance ----------
    COMMANDES = {
        "advert": "advert",
        "reboot": "reboot",
        "poweroff": "poweroff",
        "powersaving on": "powersaving on",
        "powersaving off": "powersaving off",
        "start ota": "start ota",
        "ver": "ver",
        "clock sync": "clock sync",
        "clock": "clock",
    }

    def commande(self, node_id: str, cmd: str, operateur: str = "") -> dict:
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        cmd = (cmd or "").strip().lower()
        m = re.fullmatch(r"set tx (\d{1,2})", cmd)
        if m:
            if not 1 <= int(m.group(1)) <= 22:
                return {"ok": False, "erreur": "Puissance hors limites (1 à 22 dBm)."}
        elif cmd not in self.COMMANDES:
            return {"ok": False, "erreur": "Commande non autorisée."}
        node = self.store.get_node(node_id)
        if node is None:
            return {"ok": False, "erreur": "Nœud inconnu."}
        if not self.store.password(node_id):
            return {"ok": False, "erreur": "Saisissez d'abord le mot de passe admin de ce répéteur (Modifier)."}
        self.prioritaire += 1
        try:
            return self._submit(self._commande_prio(node, cmd, operateur), timeout=260)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Commande impossible : {e}"}
        finally:
            self.prioritaire = max(0, self.prioritaire - 1)

    # ---------- découverte des voisins ----------
    def decouvrir_voisins(self, node_id: str, operateur: str = "") -> dict:
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        node = self.store.get_node(node_id)
        if node is None:
            return {"ok": False, "erreur": "Nœud inconnu."}
        if not self.store.password(node_id):
            return {"ok": False, "erreur": "Saisissez d'abord le mot de passe admin de ce répéteur (Modifier)."}
        self.prioritaire += 1
        try:
            return self._submit(self._decouvrir_voisins(node, operateur), timeout=180)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Découverte impossible : {e}"}
        finally:
            self.prioritaire = max(0, self.prioritaire - 1)

    async def _decouvrir_voisins(self, node: dict, operateur: str) -> dict:
        if not await self._attendre_la_main("Découverte des voisins"):
            return {"ok": False, "erreur": "L'interrogation en cours ne s'est pas terminée à temps. Réessayez."}
        name = node.get("nom", "")
        contact = self._contact_for(name)
        if contact is None:
            return {"ok": False, "erreur": f"{name} n'est pas dans les contacts du nœud companion."}
        login = await self.mc.commands.send_login_sync(contact, self.store.password(node["id"]), min_timeout=8)
        if not (login and login.type == EventType.LOGIN_SUCCESS):
            return {"ok": False, "erreur": f"Connexion admin refusée par {name} (mot de passe ou portée)."}
        avant = {v.get("nom") for v in node.get("voisins") or []}
        rep = await self._cli(contact, "discover.neighbors", timeout=20)
        if rep and re.search(r"unknown|inconnu|err", rep, re.I):
            return {"ok": False, "erreur": f"{name} ne connaît pas la commande discover.neighbors (firmware trop ancien ?) : {rep.strip()}"}
        await asyncio.sleep(20)  # les voisins répondent par une annonce sans relais (zero hop)
        nb = None
        for _essai in range(2):
            try:
                nb = await self.mc.commands.fetch_all_neighbours(contact, min_timeout=12)
            except Exception:  # noqa: BLE001
                nb = None
            if nb and isinstance(nb.get("neighbours"), list):
                break
        if not (nb and isinstance(nb.get("neighbours"), list)):
            return {"ok": False, "erreur": f"Découverte lancée ({(rep or 'pas de réponse').strip()}), mais la liste des voisins n'a pas pu être relue. Réessayez dans une minute."}
        voisins = [{"nom": self._name_for_prefix(v.get("pubkey", "")), "snr": v.get("snr"), "ilYa": v.get("secs_ago")}
                   for v in nb["neighbours"]]
        nouveaux = [v["nom"] for v in voisins if v["nom"] not in avant]
        self.store.patch_node(node["id"], {"voisins": voisins, "voisinsDecouverte": now_iso(), "voisinsNouveaux": nouveaux})
        texte = f"Découverte des voisins : {len(voisins)} voisin(s)" + (f", nouveaux : {', '.join(nouveaux)}" if nouveaux else ", aucun nouveau")
        self.store.add_journal(name, operateur or "PC", texte + ".")
        return {"ok": True, "message": texte, "voisins": voisins, "nouveaux": nouveaux}

    # ---------- console distante ----------
    def console(self, node_id: str, cmd: str, operateur: str = "") -> dict:
        """Envoie une commande console libre à un nœud (droits admin) et renvoie sa réponse."""
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        cmd = (cmd or "").strip()
        if not cmd:
            return {"ok": False, "erreur": "Commande vide."}
        if len(cmd.encode("utf-8")) > 140:
            return {"ok": False, "erreur": "Commande trop longue (140 octets maximum)."}
        node = self.store.get_node(node_id)
        if node is None:
            return {"ok": False, "erreur": "Nœud inconnu."}
        if not self.store.password(node_id):
            return {"ok": False, "erreur": "Saisissez d'abord le mot de passe admin de ce nœud (Modifier)."}
        if cmd.lower() in ("reboot", "poweroff", "shutdown"):
            return self.commande(node_id, "poweroff" if cmd.lower() == "shutdown" else cmd.lower(), operateur)
        self.prioritaire += 1
        self.console_etape = "en file d'attente de la liaison radio…"
        log.info("Console %s : « %s » demandée", node.get("nom"), cmd)
        fut = self._submit(self._console(node, cmd, operateur))
        self._console_fut = fut
        try:
            return fut.result(300)
        except concurrent.futures.CancelledError:
            return {"ok": False, "erreur": "Commande annulée."}
        except concurrent.futures.TimeoutError:
            fut.cancel()
            return {"ok": False, "erreur": "Pas de réponse au bout de 5 minutes : commande abandonnée."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Commande impossible : {e}"}
        finally:
            self._console_fut = None
            self.prioritaire = max(0, self.prioritaire - 1)
            self.console_etape = ""

    def console_annuler(self) -> dict:
        fut = getattr(self, "_console_fut", None)
        if fut is None or fut.done():
            return {"ok": False, "erreur": "Aucune commande en cours."}
        fut.cancel()
        return {"ok": True}

    def _etape(self, txt: str):
        self.console_etape = txt
        log.info("Console : %s", txt)
        self.store._bump()

    async def _console(self, node: dict, cmd: str, operateur: str) -> dict:
        self._etape("préparation…")
        if self.busy:
            self._etape("attente de la fin de l'interrogation automatique du répéteur en cours (jusqu'à 2 min 30)")
        if not await self._attendre_la_main("Console distante"):
            return {"ok": False, "erreur": "L'interrogation automatique en cours ne s'est pas terminée à temps. Réessayez."}
        name = node.get("nom", "")
        contact = self._contact_for(name)
        if contact is None:
            self._etape(f"{name} absent des contacts : relecture des contacts du nœud companion…")
            try:
                res = await asyncio.wait_for(self.mc.commands.get_contacts(), 45)
            except asyncio.TimeoutError:
                res = None
            if res is not None and res.type != EventType.ERROR:
                self._contacts = dict(res.payload or {})
            contact = self._contact_for(name)
        if contact is None:
            return {"ok": False, "erreur": f"{name} n'est pas dans les contacts du nœud companion."}
        t0 = time.time()
        sessions = getattr(self, "_sessions_console", {})
        self._sessions_console = sessions
        if time.time() - sessions.get(node["id"], 0) > 120:  # reconnexion admin toutes les 2 minutes au plus
            self._etape(f"connexion admin à {name}…")
            login = await self._login_console(contact, node)
            if not (login and login.type == EventType.LOGIN_SUCCESS):
                sessions.pop(node["id"], None)
                return {"ok": False, "erreur": f"Connexion admin refusée par {name} (mot de passe ou portée)."}
        sessions[node["id"]] = time.time()
        if cmd.strip().lower() in ("neighbors", "neighbours"):
            # la liste des voisins est lue par la requête binaire dédiée : la réponse texte est souvent trop longue pour la radio
            self._etape("lecture de la liste des voisins…")
            nb = None
            for _essai in range(2):
                try:
                    nb = await self.mc.commands.fetch_all_neighbours(contact, min_timeout=12)
                except Exception:  # noqa: BLE001
                    nb = None
                if nb and isinstance(nb.get("neighbours"), list):
                    break
            if nb and isinstance(nb.get("neighbours"), list):
                lignes = []
                for v in sorted(nb["neighbours"], key=lambda x: x.get("secs_ago") or 0):
                    s = v.get("secs_ago")
                    quand = "" if s is None else (f"il y a {s} s" if s < 60 else f"il y a {s // 60} min" if s < 3600 else f"il y a {s // 3600} h")
                    snr = v.get("snr")
                    lignes.append(f"{self._name_for_prefix(v.get('pubkey', ''))}  SNR {snr:+.2f} dB  {quand}" if isinstance(snr, (int, float))
                                  else f"{self._name_for_prefix(v.get('pubkey', ''))}  {quand}")
                reponse = f"{len(lignes)} voisin(s)\n" + "\n".join(lignes) if lignes else "Aucun voisin entendu."
                self.store.patch_node(node["id"], {"voisins": [{"nom": self._name_for_prefix(v.get("pubkey", "")), "snr": v.get("snr"),
                                                                "ilYa": v.get("secs_ago")} for v in nb["neighbours"]]})
            else:
                reponse = None
        else:
            self._etape("commande envoyée, attente de la réponse du nœud…")
            reponse = await self._cli(contact, cmd, timeout=25)
            if reponse is None:  # la session a peut-être expiré : une reconnexion et un 2e essai
                self._etape("pas de réponse : reconnexion admin et 2e essai…")
                login = await self._login_console(contact, node)
                if login and login.type == EventType.LOGIN_SUCCESS:
                    sessions[node["id"]] = time.time()
                    reponse = await self._cli(contact, cmd, timeout=25)
                else:
                    sessions.pop(node["id"], None)
        duree = round(time.time() - t0, 1)
        histo = list(node.get("console") or [])[-49:]
        histo.append({"date": now_iso(), "cmd": cmd, "reponse": reponse, "operateur": operateur or ""})
        self.store.patch_node(node["id"], {"console": histo})
        self.store.add_journal(name, operateur or "PC", f"Console : « {cmd} »" + (f" → {reponse.strip()[:200]}" if reponse else " → pas de réponse"))
        h = self._horloge(reponse) if cmd.strip().lower() == "clock" and reponse else {}
        if h:
            self.store.patch_node(node["id"], h)
        if cmd.strip().lower() == "ver" and reponse:
            m = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", reponse)
            if m:
                self.store.patch_node(node["id"], {"firmware": "v" + m.group(1)})
        return {"ok": reponse is not None, "reponse": reponse or "", "duree": duree,
                **({} if reponse is not None else {"erreur": "Pas de réponse du nœud (portée, ou commande inconnue)."})}

    async def _login_console(self, contact, node):
        try:
            return await asyncio.wait_for(self.mc.commands.send_login_sync(contact, self.store.password(node["id"]), min_timeout=8), 40)
        except asyncio.TimeoutError:
            log.warning("Console : connexion admin sans réponse (%s)", node.get("nom"))
            return None

    # ---------- réinitialisation du chemin ----------
    def reinit_chemin(self, node_id: str, operateur: str = "") -> dict:
        """Oublie la route mémorisée par le nœud companion vers ce nœud ; la suivante est réapprise en inondation."""
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        node = self.store.get_node(node_id)
        if node is None:
            return {"ok": False, "erreur": "Nœud inconnu."}
        self.prioritaire += 1
        try:
            return self._submit(self._reinit_chemin(node, operateur), timeout=120)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Réinitialisation impossible : {e}"}
        finally:
            self.prioritaire = max(0, self.prioritaire - 1)

    async def _reinit_chemin(self, node: dict, operateur: str) -> dict:
        if not await self._attendre_la_main("Réinitialisation du chemin"):
            return {"ok": False, "erreur": "L'interrogation en cours ne s'est pas terminée à temps. Réessayez."}
        name = node.get("nom", "")
        contact = self._contact_for(name)
        if contact is None:
            res = await self.mc.commands.get_contacts()
            if res is not None and res.type != EventType.ERROR:
                self._contacts = dict(res.payload or {})
            contact = self._contact_for(name)
        if contact is None:
            return {"ok": False, "erreur": f"{name} n'est pas dans les contacts du nœud companion."}
        avant = self._chemin(contact)
        ev = await self.mc.commands.reset_path(contact)
        if ev is None or ev.type == EventType.ERROR:
            return {"ok": False, "erreur": "Le nœud companion a refusé la réinitialisation."}
        contact["out_path_len"], contact["out_path"] = -1, ""
        self.store.patch_node(node["id"], {"chemin": self._chemin(contact), "cheminDate": now_iso()})
        # on sollicite le nœud pour qu'un nouveau chemin soit appris tout de suite (répéteurs uniquement)
        apres = None
        if contact.get("type") in (2, 3):
            try:
                await self.mc.commands.req_status_sync(contact, min_timeout=15)
            except Exception:  # noqa: BLE001
                pass
            res = await self.mc.commands.get_contacts()
            if res is not None and res.type != EventType.ERROR:
                self._contacts = dict(res.payload or {})
            c2 = self._contact_for(name)
            if c2 is not None:
                apres = self._chemin(c2)
                self.store.patch_node(node["id"], {"chemin": apres, "cheminDate": now_iso()})
        def txt(c):
            if not c or c["type"] == "inondation":
                return "inondation (route inconnue)"
            return "direct" if c["type"] == "direct" else " → ".join(c["relais"])
        msg = f"Chemin réinitialisé (avant : {txt(avant)})" + (f" ; nouveau chemin : {txt(apres)}" if apres else " ; il sera réappris au prochain échange")
        self.store.add_journal(name, operateur or "AUTO", msg + ".")
        return {"ok": True, "message": msg, "chemin": apres}

    async def _commande_prio(self, node, cmd, operateur):
        if not await self._attendre_la_main("Commande"):
            return {"ok": False, "erreur": "L'interrogation en cours ne s'est pas terminée à temps. Réessayez."}
        return await self._commande(node, cmd, operateur)

    async def _commande(self, node: dict, cmd: str, operateur: str) -> dict:
        name = node.get("nom", "")
        contact = self._contact_for(name)
        if contact is None:
            res = await self.mc.commands.get_contacts()
            if res is not None and res.type != EventType.ERROR:
                self._contacts = dict(res.payload or {})
            contact = self._contact_for(name)
        if contact is None:
            return {"ok": False, "erreur": f"{name} n'est pas dans les contacts du nœud companion."}
        login = await self.mc.commands.send_login_sync(contact, self.store.password(node["id"]), min_timeout=8)
        if not (login and login.type == EventType.LOGIN_SUCCESS):
            return {"ok": False, "erreur": f"Connexion admin refusée par {name} (mot de passe ou portée)."}
        if cmd in ("reboot", "poweroff"):
            return await self._arret(node, contact, cmd, operateur)
        reponse = await self._cli(contact, cmd, timeout=25)
        texte = f"Commande « {cmd} » envoyée à {name}" + (f" — réponse : {reponse.strip()}" if reponse else " — pas de réponse")
        self.store.add_journal(name, operateur or "AUTO", texte)
        if reponse and cmd.startswith("set tx"):
            self.store.patch_node(node["id"], {"tx": int(cmd.split()[-1])})
        if cmd == "start ota":
            self.store.patch_node(node["id"], {"otaDebut": now_iso(), "otaFirmwareAvant": node.get("firmware", ""),
                                               "otaReponse": (reponse or "").strip()})
        if cmd == "clock sync" and reponse:
            reponse_h = await self._cli(contact, "clock")
            self.store.patch_node(node["id"], self._horloge(reponse_h))
        if cmd == "clock":
            self.store.patch_node(node["id"], self._horloge(reponse))
        if cmd == "ver" and reponse:
            m = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", reponse)
            if m:
                self._confirmer_ota(self.store.get_node(node["id"]) or node, "v" + m.group(1), operateur)
        self._log(texte)
        return {"ok": True, "reponse": reponse or ""}


    # ---------- équipes (companions qui partagent leur position) ----------
    def _update_equipes(self):
        amis = set(self.store.get_config("amis", []) or [])
        out = []
        for c in self._contacts.values():
            if c.get("type") != 1:
                continue
            lat, lon = c.get("adv_lat") or 0, c.get("adv_lon") or 0
            if abs(lat) < 0.01 or abs(lon) < 0.01:
                continue
            cle = c.get("public_key", "")[:12]
            ts = c.get("last_advert") or time.time()
            self.store.add_position(cle, c.get("adv_name", ""), lat, lon, ts)
            out.append({"cle": cle, "nom": c.get("adv_name", ""), "lat": lat, "lon": lon,
                        "ami": c.get("public_key", "") in amis,
                        "vu": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))})
        out.sort(key=lambda x: x["nom"].lower())
        self.equipes = out

    async def _equipes_loop(self):
        """Relit les contacts toutes les 60 s pour suivre les positions des équipes."""
        while self.mc is not None:
            await asyncio.sleep(60)
            if self.busy or self.couverture_en_cours or self.mc is None:
                continue
            try:
                await self._maj_position()
                await self._maj_batterie()
                res = await self.mc.commands.get_contacts()
                if res is not None and res.type != EventType.ERROR:
                    self._contacts = dict(res.payload or {})
                    self._update_equipes()
                    self.store._bump()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("équipes : %s", e)

    # ---------- test de couverture ----------
    def test_couverture(self, lat, lon, lieu: str) -> dict:
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        if self.couverture_en_cours:
            return {"ok": False, "erreur": "Un test de couverture est déjà en cours."}
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return {"ok": False, "erreur": "Position invalide : indiquez la latitude et la longitude du point de mesure."}
        self.prioritaire += 1
        try:
            return self._submit(self._couverture_prio(lat, lon, lieu), timeout=1100)
        except Exception as e:  # noqa: BLE001
            self.couverture_en_cours = False
            return {"ok": False, "erreur": f"Test impossible : {e}"}
        finally:
            self.prioritaire = max(0, self.prioritaire - 1)

    async def _couverture_prio(self, lat, lon, lieu):
        if not await self._attendre_la_main("Test de couverture"):
            return {"ok": False, "erreur": "L'interrogation en cours ne s'est pas terminée à temps. Réessayez."}
        return await self._couverture(lat, lon, lieu)

    async def _trace(self, contact) -> dict | None:
        """Trace vers un répéteur voisin direct : SNR reçu par le répéteur (aller) et par nous (retour)."""
        mc = self.mc
        h = contact.get("public_key", "")[:2]
        tag = random.randint(1, 0xFFFFFFFF)
        waiter = asyncio.ensure_future(mc.dispatcher.wait_for_event(EventType.TRACE_DATA, attribute_filters={"tag": tag}, timeout=15))
        await asyncio.sleep(0)
        sent = await mc.commands.send_trace(tag=tag, path=h)
        if sent is None or sent.type == EventType.ERROR:
            waiter.cancel()
            return None
        ev = await waiter
        if not ev:
            return None
        path = (ev.payload or {}).get("path") or []
        if len(path) >= 2:
            return {"aller": path[0].get("snr"), "retour": path[-1].get("snr")}
        return None

    async def _couverture(self, lat, lon, lieu):
        self.couverture_en_cours = True
        self.message = "Test de couverture en cours…"
        self.store._bump()
        try:
            res = await self.mc.commands.get_contacts()
            if res is not None and res.type != EventType.ERROR:
                self._contacts = dict(res.payload or {})
            cibles = [n for n in self.store.list_nodes() if n.get("proprio") != "externe" and n.get("statut") not in ("prevu", "supprime")]
            resultats = []
            for i, node in enumerate(cibles, 1):
                self.message = f"Test de couverture {i}/{len(cibles)} : {node.get('nom')}"
                self.store._bump()
                c = self._contact_for(node.get("nom", ""))
                r = {"nom": node.get("nom"), "joignable": False, "sauts": None, "aller": None, "retour": None, "delai": None}
                if c is None:
                    r["remarque"] = "absent des contacts"
                    resultats.append(r)
                    continue
                t0 = time.time()
                tr = None
                try:
                    tr = await self._trace(c)
                except Exception as e:  # noqa: BLE001
                    log.debug("trace %s : %s", node.get("nom"), e)
                if tr:
                    r.update(joignable=True, sauts=0, aller=tr["aller"], retour=tr["retour"], delai=round(time.time() - t0, 1))
                else:
                    try:
                        st = await self.mc.commands.req_status_sync(c, min_timeout=10)
                    except Exception:
                        st = None
                    if st:
                        ch = self._chemin(c)
                        r.update(joignable=True, sauts=ch.get("sauts"), delai=round(time.time() - t0, 1),
                                 remarque="via le réseau" if ch.get("sauts") else "joignable")
                if r["joignable"]:
                    self._marquer_vivant(node)
                resultats.append(r)
            cid = self.store.add_coverage(lat, lon, lieu or "", resultats)
            ok = sum(1 for r in resultats if r["joignable"])
            self.store.add_journal("", "AUTO", f"Test de couverture « {lieu or 'sans nom'} » : {ok}/{len(resultats)} répéteurs joignables.")
            self._log(f"Test de couverture terminé : {ok}/{len(resultats)} joignables")
            return {"ok": True, "id": cid, "resultats": resultats}
        finally:
            self.couverture_en_cours = False
            self.message = "Test de couverture terminé"
            self.store._bump()


    # ---------- mise à jour du firmware (OTA Wi-Fi) ----------
    @staticmethod
    def _horloge(reponse) -> dict:
        """« 21:27 - 12/10/2026 UTC » (réponse de la commande clock) → heure du nœud et écart avec le PC."""
        m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})", reponse or "")
        if not m:
            return {}
        import datetime as _dt
        hh, mi, ss, j, mo, an = m.groups()
        try:
            t = _dt.datetime(int(an), int(mo), int(j), int(hh), int(mi), int(ss or 0), tzinfo=_dt.timezone.utc)
        except ValueError:
            return {}
        pc = _dt.datetime.now(_dt.timezone.utc)
        ecart = (t - pc).total_seconds()
        if ss is None:  # réponse à la minute près : écart nul si le PC est dans la même minute
            ecart = 0 if -60 < ecart <= 0 else round((ecart + 30) / 60) * 60
        return {"horloge": t.isoformat(), "horlogeEcart": int(ecart), "horlogeSecondes": ss is not None,
                "horlogeLue": now_iso()}

    def _confirmer_ota(self, node: dict, nouvelle: str, operateur: str):
        """Après un « start ota », note la nouvelle version quand elle change."""
        avant = node.get("otaFirmwareAvant") or node.get("firmware") or "?"
        fields = {"firmware": nouvelle}
        if node.get("otaDebut") and nouvelle != avant:
            self.store.add_journal(node.get("nom", ""), operateur or "AUTO",
                                   f"Mise à jour du firmware confirmée : {avant} → {nouvelle}.")
            self.alerte("info", node.get("nom", ""), f"{node.get('nom')} : firmware mis à jour en {nouvelle}.")
            fields.update(otaDebut="", otaReponse="")
        self.store.patch_node(node["id"], fields)


    # ---------- priorité aux demandes de l'opérateur ----------
    async def _attendre_la_main(self, quoi: str, delai: float = 150) -> bool:
        """L'interrogation en cours s'arrête après le répéteur en cours ; on attend qu'elle ait rendu la main."""
        t0 = time.time()
        while self.busy and time.time() - t0 < delai:
            self.message = f"{quoi} : attente de la fin de l'interrogation du répéteur en cours…"
            self.store._bump()
            await asyncio.sleep(1)
        return not self.busy


    # ---------- position GPS du nœud companion ----------
    # ---------- alerte précoce et autonomie ----------
    def _analyse(self, node: dict, fields: dict) -> dict:
        """Tendances sur l'historique : bruit, erreurs RX, batterie, liaisons ; autonomie des nœuds sur batterie."""
        hist = self.store.history(node["id"], 30)
        now = time.time()
        out, alertes = {}, []

        def med(v):
            v = sorted(x for x in v if x is not None)
            return v[len(v) // 2] if v else None

        recent = [h for h in hist if h["ts"] >= now - 86400]
        base = [h for h in hist if now - 8 * 86400 <= h["ts"] < now - 86400]
        if len(recent) >= 2 and len(base) >= 4:
            r, b = med([h["bruit"] for h in recent]), med([h["bruit"] for h in base])
            if r is not None and b is not None and r - b >= 6:
                alertes.append(("bruit", f"Bruit en hausse : {r:.0f} dBm (habituel {b:.0f} dBm), brouillage ou antenne à vérifier"))

        def taux(lst):
            n, dt = 0, 0.0
            for a, b2 in zip(lst, lst[1:]):
                if a["erreurs"] is None or b2["erreurs"] is None:
                    continue
                d = b2["erreurs"] - a["erreurs"]
                if d >= 0:
                    n += d
                    dt += b2["ts"] - a["ts"]
            return n / (dt / 3600) if dt > 600 else None
        tr, tb = taux(recent), taux(base)
        if tr is not None and tr >= 20 and (tb is None or tr >= 3 * max(tb, 1)):
            alertes.append(("erreurs", f"Erreurs de réception en forte hausse : {tr:.0f}/h"
                            + (f" (habituel {tb:.0f}/h)" if tb is not None else "")))

        jours = {}
        for h in hist:
            if h["bat_mv"]:
                j = time.strftime("%Y-%m-%d", time.localtime(h["ts"]))
                jours[j] = min(jours.get(j, 99999), h["bat_mv"])
        serie = [(j, v / 1000) for j, v in sorted(jours.items())]
        if len(serie) >= 3:
            der = serie[-7:]
            xs = list(range(len(der)))
            ys = [v for _, v in der]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            den = sum((x - mx) ** 2 for x in xs) or 1
            pente = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
            plancher = 3.3
            jours_restants = None
            if pente < -0.005 and ys[-1] > plancher:
                jours_restants = int((ys[-1] - plancher) / -pente)
            out["autonomie"] = {"pente": round(pente, 4), "jours": jours_restants, "minJour": round(ys[-1], 2),
                                "serie": [[j, round(v, 3)] for j, v in serie[-30:]], "calcule": now_iso()}
            baisse = len(ys) >= 4 and all(b2 <= a + 0.005 for a, b2 in zip(ys[-4:], ys[-3:])) and ys[-4] - ys[-1] >= 0.08
            if baisse and pente <= -0.03:
                alertes.append(("batterie", f"Batterie en baisse continue : {pente * 1000:+.0f} mV/jour"
                                + (f", autonomie estimée ≈ {jours_restants} j" if jours_restants is not None else "")))

        ref = dict(node.get("voisinsRef") or {})
        for v in fields.get("voisins") or []:
            nom, snr = v.get("nom"), v.get("snr")
            if nom is None or snr is None:
                continue
            avant = ref.get(nom)
            if avant is not None and avant - snr >= 6:
                alertes.append((f"lien:{nom}", f"Liaison avec {nom} affaiblie : {snr:+.1f} dB (habituel {avant:+.1f} dB)"))
            ref[nom] = round(snr if avant is None else 0.8 * avant + 0.2 * snr, 2)
        if fields.get("voisins") is not None:
            out["voisinsRef"] = ref

        anciennes = {t["cle"]: t for t in (node.get("tendances") or [])}
        tend = []
        for cle, texte in alertes:
            depuis = anciennes.get(cle, {}).get("depuis") or now_iso()
            tend.append({"cle": cle, "texte": texte, "depuis": depuis})
            if cle not in anciennes:
                nom = node.get("nom", "")
                self.store.add_journal(nom, "AUTO", "Alerte précoce : " + texte + ".", auto=True)
                self.alerte("attention", nom, f"{nom} : {texte}.")
        out["tendances"] = tend
        return out

    # ---------- test de liaison (trace aller-retour) ----------
    def tester_liaison(self, node_id: str, operateur: str = "") -> dict:
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        node = self.store.get_node(node_id)
        if node is None:
            return {"ok": False, "erreur": "Nœud inconnu."}
        self.prioritaire += 1
        try:
            return self._submit(self._tester_liaison(node, operateur), timeout=150)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Test impossible : {e}"}
        finally:
            self.prioritaire = max(0, self.prioritaire - 1)

    async def _tester_liaison(self, node: dict, operateur: str) -> dict:
        if not await self._attendre_la_main("Test de liaison"):
            return {"ok": False, "erreur": "L'interrogation en cours ne s'est pas terminée à temps. Réessayez."}
        mc, name = self.mc, node.get("nom", "")
        contact = self._contact_for(name)
        if contact is None:
            return {"ok": False, "erreur": f"{name} n'est pas dans les contacts du nœud companion."}
        n = contact.get("out_path_len", -1)
        taille = max(1, (contact.get("out_path_hash_mode") or 0) + 1)
        if n is None or n < 0:
            hops, note = [], "Route inconnue : test en direct (sans relais)."
        else:
            hexpath = contact.get("out_path") or ""
            hops = [hexpath[i:i + 2 * taille] for i in range(0, len(hexpath), 2 * taille)][:n]
            note = ""
        cible = contact.get("public_key", "")[:2 * taille]
        chemin = hops + [cible] + list(reversed(hops))
        noms = self._chemin(contact).get("relais", []) if hops else []
        moi = self.local_name or "Ce nœud"
        etiquettes = [moi] + noms + [name] + list(reversed(noms)) + [moi]
        tag = random.randint(1, 0xFFFFFFFF)
        waiter = asyncio.ensure_future(mc.dispatcher.wait_for_event(
            EventType.TRACE_DATA, attribute_filters={"tag": tag}, timeout=20 + 6 * len(chemin)))
        await asyncio.sleep(0)
        t0 = time.time()
        sent = await mc.commands.send_trace(tag=tag, path=",".join(chemin))
        if sent is None or sent.type == EventType.ERROR:
            waiter.cancel()
            res = {"ok": False, "date": now_iso(), "note": "Le nœud companion a refusé l'envoi du test.", "segments": []}
        else:
            ev = await waiter
            path = ((ev.payload or {}).get("path") or []) if ev else []
            if not ev or len(path) < 2:
                res = {"ok": False, "date": now_iso(), "segments": [],
                       "note": (note + " " if note else "") + "Pas de retour : la liaison est coupée quelque part sur le trajet."}
            else:
                segs = [{"de": etiquettes[i] if i < len(etiquettes) else "?",
                         "vers": etiquettes[i + 1] if i + 1 < len(etiquettes) else "?", "snr": pnt.get("snr")}
                        for i, pnt in enumerate(path)]
                pire = min((x["snr"] for x in segs if x["snr"] is not None), default=None)
                res = {"ok": True, "date": now_iso(), "segments": segs, "duree": round(time.time() - t0, 1),
                       "note": note, "pire": pire}
        self.store.patch_node(node["id"], {"testLiaison": res})
        if res["ok"] and res.get("pire") is not None:
            resume = f"OK en {res['duree']} s, maillon le plus faible {res['pire']:+.1f} dB"
        else:
            resume = res.get("note") or "échec"
        self.store.add_journal(name, operateur or "AUTO", f"Test de liaison aller-retour : {resume}.")
        return {"ok": res["ok"], "message": resume, "test": res}

    # ---------- chemin manuel vers un contact ----------
    def _contact_par_cle(self, cle: str):
        cle = (cle or "").lower()
        return next((c for c in self._contacts.values() if cle and (c.get("public_key") or "").lower().startswith(cle)), None)

    def chemin_contact(self, cle: str) -> dict:
        """Route actuelle vers un contact et liste des relais possibles (répéteurs et salons connus)."""
        c = self._contact_par_cle(cle)
        if c is None:
            return {"ok": False, "erreur": "Contact introuvable dans le nœud companion."}
        taille = max(1, (c.get("out_path_hash_mode") or 0) + 1)
        n = c.get("out_path_len", -1)
        hexpath = c.get("out_path") or ""
        hops = [hexpath[i:i + 2 * taille] for i in range(0, len(hexpath), 2 * taille)][:max(0, n or 0)] if n and n > 0 else []
        relais = []
        for c2 in self._contacts.values():
            if c2.get("type") in (2, 3) and c2.get("public_key"):
                relais.append({"cle": c2["public_key"], "nom": c2.get("adv_name") or c2["public_key"][:8],
                               "lat": c2.get("adv_lat") if abs(c2.get("adv_lat") or 0) > .01 else None,
                               "lon": c2.get("adv_lon") if abs(c2.get("adv_lon") or 0) > .01 else None})
        relais.sort(key=lambda x: x["nom"].lower())
        actuels = []
        for h in hops:
            cand = [r for r in relais if r["cle"].lower().startswith(h.lower())]
            actuels.append({"hash": h, "cle": cand[0]["cle"] if len(cand) == 1 else None,
                            "nom": cand[0]["nom"] if cand else f"? {h.upper()}", "ambigu": len(cand) > 1})
        return {"ok": True, "nom": c.get("adv_name"), "cle": c.get("public_key"),
                "mode": "inondation" if n is None or n < 0 else ("direct" if n == 0 else "relais"),
                "actuels": actuels, "taille": taille, "relais": relais}

    def definir_chemin(self, cle: str, mode: str, relais: list, operateur: str = "") -> dict:
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        try:
            return self._submit(self._definir_chemin(cle, mode, relais or [], operateur), timeout=60)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Chemin impossible à enregistrer : {e}"}

    async def _definir_chemin(self, cle: str, mode: str, relais: list, operateur: str) -> dict:
        c = self._contact_par_cle(cle)
        if c is None:
            return {"ok": False, "erreur": "Contact introuvable dans le nœud companion."}
        nom = c.get("adv_name", "")
        if mode == "inondation":
            ev = await self.mc.commands.reset_path(c)
            if ev is None or ev.type == EventType.ERROR:
                return {"ok": False, "erreur": "Le nœud companion a refusé la réinitialisation."}
            c["out_path_len"], c["out_path"] = -1, ""
            texte = "inondation (le réseau cherchera la route)"
        else:
            hmode = 0
            try:
                dq = await self.mc.commands.send_device_query()
                if dq is not None and dq.type != EventType.ERROR:
                    hmode = int((dq.payload or {}).get("path_hash_mode") or 0)
            except Exception:  # noqa: BLE001
                pass
            taille = hmode + 1
            hops = []
            if mode != "direct":
                for r in relais:
                    r = re.sub(r"[^0-9a-fA-F]", "", str(r)).lower()
                    if len(r) < 2 * taille:
                        return {"ok": False, "erreur": f"Identifiant de relais trop court : {r or '?'} ({2 * taille} caractères hexadécimaux attendus)."}
                    hops.append(r[:2 * taille])
                if not hops:
                    return {"ok": False, "erreur": "Choisissez au moins un relais, ou le mode direct."}
                if len(hops) * taille > 64:
                    return {"ok": False, "erreur": "Chemin trop long (64 octets au maximum)."}
            ev = await self.mc.commands.change_contact_path(c, "".join(hops), path_hash_mode=hmode)
            if ev is None or ev.type == EventType.ERROR:
                return {"ok": False, "erreur": "Le nœud companion a refusé le nouveau chemin."}
            texte = "direct (sans relais)" if not hops else " → ".join(self._chemin(c).get("relais") or hops)
        self.store.add_journal(nom, operateur or "PC", f"Chemin vers {nom} défini manuellement : {texte}.")
        self.store._bump()
        return {"ok": True, "message": f"Chemin vers {nom} : {texte}", "chemin": self._chemin(c)}

    # ---------- régions (portée des messages en inondation) ----------
    def regions(self, lire_repeteurs: bool = False) -> dict:
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        try:
            return self._submit(self._regions(lire_repeteurs), timeout=240)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Lecture impossible : {e}"}

    async def _regions(self, lire_repeteurs: bool) -> dict:
        defaut = ""
        try:
            ev = await self.mc.commands.get_default_flood_scope()
            if ev is not None and ev.type != EventType.ERROR:
                defaut = (ev.payload or {}).get("scope_name") or ""
        except Exception:  # noqa: BLE001
            pass
        self.store.set_config("region_defaut", defaut)
        par_rep = dict(self.store.get_config("regions_repeteurs", {}) or {})
        if lire_repeteurs:
            if not await self._attendre_la_main("Lecture des régions"):
                return {"ok": False, "erreur": "L'interrogation en cours ne s'est pas terminée à temps. Réessayez."}
            for n in self.store.list_nodes():
                if n.get("proprio") == "externe" or n.get("statut") in ("prevu", "supprime") or n.get("eteint"):
                    continue
                c = self._contact_for(n.get("nom", ""))
                if c is None:
                    continue
                try:
                    txt = await self.mc.commands.req_regions_sync(c, min_timeout=10)
                except Exception:  # noqa: BLE001
                    txt = None
                if txt is not None:
                    par_rep[n["nom"]] = [r for r in re.split(r"[\s,;]+", txt) if r]
            self.store.set_config("regions_repeteurs", par_rep)
        self.store._bump()
        return {"ok": True, **self.etat_regions()}

    def etat_regions(self) -> dict:
        par_rep = self.store.get_config("regions_repeteurs", {}) or {}
        connues = sorted({r if r.startswith("#") else "#" + r for l in par_rep.values() for r in l if r not in ("*", "-")})
        return {"defaut": self.store.get_config("region_defaut", "") or "", "parRepeteur": par_rep,
                "connues": connues, "parCanal": self.store.get_config("regions_canaux", {}) or {}}

    def regler_region(self, canal=None, region: str = "", defaut: bool = False) -> dict:
        region = (region or "").strip()
        if region and region != "*" and not region.startswith("#"):
            region = "#" + region
        if defaut:
            if self.mc is None:
                return {"ok": False, "erreur": "Aucun nœud connecté."}
            try:
                ev = self._submit(self.mc.commands.set_default_flood_scope(region or None), timeout=20)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "erreur": f"Réglage impossible : {e}"}
            if ev is None or ev.type == EventType.ERROR:
                return {"ok": False, "erreur": "Le nœud companion a refusé la région (firmware trop ancien ?)."}
            self.store.set_config("region_defaut", region)
        else:
            rc = dict(self.store.get_config("regions_canaux", {}) or {})
            if region:
                rc[str(int(canal))] = region
            else:
                rc.pop(str(int(canal)), None)
            self.store.set_config("regions_canaux", rc)
        self.store._bump()
        return {"ok": True, **self.etat_regions()}

    # ---------- analyse du nœud companion ----------
    RESEAU = {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 8}

    @staticmethod
    def _decalage_pc() -> int:
        """Décalage horaire actuel du PC en heures (Paris : +1 en hiver, +2 en été)."""
        from datetime import datetime
        off = datetime.now().astimezone().utcoffset()
        return int(round(off.total_seconds() / 3600)) if off is not None else 0

    async def _fuseau_ecran(self):
        """L'écran du companion affiche l'heure UTC + « tz.offset » (heures entières, sans changement d'heure automatique).
        On l'aligne sur le décalage du PC. Renvoie (avant, après) ou None si le firmware ne le permet pas."""
        try:
            ev = await asyncio.wait_for(self.mc.commands.run_cli_command("get tz.offset"), 8)
        except Exception:  # noqa: BLE001
            return None
        if ev is None or ev.type == EventType.ERROR:
            return None
        m = re.search(r"(-?\d+)", (ev.payload or {}).get("text", ""))
        if not m:
            return None
        avant, voulu = int(m.group(1)), self._decalage_pc()
        if avant == voulu:
            return avant, voulu
        try:
            ev = await asyncio.wait_for(self.mc.commands.run_cli_command(f"set tz.offset {voulu}"), 8)
        except Exception:  # noqa: BLE001
            return None
        if ev is None or ev.type == EventType.ERROR or "ok" not in (ev.payload or {}).get("text", "").lower():
            return None
        log.info("Fuseau de l'écran du companion : %+d → %+d", avant, voulu)
        return avant, voulu

    def heure_companion(self, operateur: str = "PC") -> dict:
        """Met l'horloge du nœud companion à l'heure du PC et indique l'écart corrigé."""
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}

        async def lire():
            try:
                ev = await asyncio.wait_for(self.mc.commands.get_time(), 10)
                if ev is not None and ev.type != EventType.ERROR:
                    return (ev.payload or {}).get("time")
            except Exception:  # noqa: BLE001
                pass
            return None

        async def go():
            avant = await lire()
            ev = await asyncio.wait_for(self.mc.commands.set_time(int(time.time())), 10)
            fz = await self._fuseau_ecran()
            if ev is None or ev.type == EventType.ERROR:
                return None, avant, None, fz
            return True, avant, await lire(), fz
        try:
            ok, avant, apres, fz = self._submit(go(), timeout=60)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Mise à l'heure impossible : {e}"}
        ecart = int(avant - time.time()) if isinstance(avant, (int, float)) else None
        if not ok and fz is not None and fz[0] != fz[1]:
            msg = (f"Écran du companion corrigé : UTC{fz[0]:+d} → UTC{fz[1]:+d} (changement d'heure). "
                   f"L'horloge interne avance de {ecart} s et n'a pas pu être reculée." if ecart and ecart > 1 else
                   f"Écran du companion corrigé : UTC{fz[0]:+d} → UTC{fz[1]:+d} (changement d'heure).")
            self.store.add_journal("", operateur, msg, nature="evenement")
            self._log(msg)
            return {"ok": True, "message": msg, "ecart": ecart}
        if not ok and ecart is not None and 0 < ecart <= 5:
            ok, apres = True, None  # avance de quelques secondes seulement : considéré à l'heure
        if not ok:
            if ecart is not None and ecart > 1:
                return {"ok": False, "erreur": f"Le nœud companion a refusé : son horloge avance de {ecart} s et MeshCore n'accepte pas "
                                              "de la reculer. Éteignez puis rallumez le nœud, reconnectez-le, puis réessayez."}
            return {"ok": False, "erreur": "Le nœud companion a refusé la mise à l'heure."}
        def txt(e):
            if e is None:
                return "écart précédent inconnu"
            a = abs(e)
            d = f"{a} s" if a < 120 else f"{a // 60} min" if a < 7200 else f"{a // 3600} h {a % 3600 // 60:02d}" if a < 172800 else f"{a // 86400} jours"
            return "était déjà à l'heure" if a <= 1 else f"il {'avançait' if e > 0 else 'retardait'} de {d}"
        heure = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(apres)) if isinstance(apres, (int, float)) else time.strftime("%d/%m/%Y %H:%M:%S")
        if fz is None:
            ecran = " Fuseau de l'écran : non réglable avec ce firmware (mettez-le à jour)."
        elif fz[0] != fz[1]:
            ecran = f" Écran corrigé : UTC{fz[0]:+d} → UTC{fz[1]:+d} ({'heure d’hiver' if fz[1] == 1 else 'heure d’été' if fz[1] == 2 else 'heure locale'})."
        else:
            ecran = f" Écran déjà à l'heure locale (UTC{fz[1]:+d})."
        msg = f"{self.local_name or 'Nœud companion'} mis à l'heure du PC : {heure} ({txt(ecart)}).{ecran}"
        self.store.add_journal("", operateur, msg, nature="evenement")
        self._log(msg)
        return {"ok": True, "message": msg, "ecart": ecart}

    def envoyer_advert(self, inondation: bool = False) -> dict:
        """Annonce (advert) du nœud companion : voisins directs seulement, ou relayée par les répéteurs (inondation)."""
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        async def go():
            return await self.mc.commands.send_advert(flood=inondation)
        try:
            ev = self._submit(go(), timeout=30)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Advert impossible : {e}"}
        if ev is None or ev.type == EventType.ERROR:
            return {"ok": False, "erreur": "Le nœud a refusé l'advert."}
        quoi = "à tout le réseau (relayé par les répéteurs)" if inondation else "aux voisins directs"
        self._log(f"Advert envoyé {quoi}")
        self.store.add_journal("", self.store.get_config("operateur", "") or "PC",
                               f"Advert du nœud companion {self.local_name or ''} envoyé {quoi}.", nature="evenement")
        self.store._bump()
        return {"ok": True, "message": f"Advert envoyé {quoi}"}

    def analyser_companion(self) -> dict:
        if self.mc is None:
            return {"ok": False, "erreur": "Aucun nœud connecté."}
        self.prioritaire += 1
        try:
            return self._submit(self._analyser_companion(), timeout=120)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Analyse impossible : {e}"}
        finally:
            self.prioritaire = max(0, self.prioritaire - 1)

    async def _analyser_companion(self) -> dict:
        if not await self._attendre_la_main("Analyse du nœud companion"):
            return {"ok": False, "erreur": "L'interrogation en cours ne s'est pas terminée à temps. Réessayez."}
        mc = self.mc
        r: dict = {"date": now_iso()}

        async def lire(cle, coro):
            try:
                ev = await coro
                if ev is not None and ev.type != EventType.ERROR:
                    r[cle] = ev.payload
            except Exception as e:  # noqa: BLE001
                log.debug("analyse %s : %s", cle, e)

        await lire("identite", mc.commands.send_appstart())
        await lire("appareil", mc.commands.send_device_query())
        await lire("batterie", mc.commands.get_bat())
        await lire("heure", mc.commands.get_time())
        h = (r.get("heure") or {}).get("time")
        if h and abs(h - time.time()) > 120:  # horloge du companion décalée : on la remet à l'heure du PC
            try:
                await mc.commands.set_time(int(time.time()))
                r["heureCorrigee"] = int(h - time.time())
                await lire("heure", mc.commands.get_time())
            except Exception:  # noqa: BLE001
                pass
        await lire("core", mc.commands.get_stats_core())
        await lire("radio", mc.commands.get_stats_radio())
        await lire("paquets", mc.commands.get_stats_packets())
        await lire("reglage", mc.commands.get_tuning())
        await lire("telemetrie", mc.commands.get_self_telemetry())
        await lire("variables", mc.commands.get_custom_vars())
        try:
            res = await mc.commands.get_contacts()
            if res is not None and res.type != EventType.ERROR:
                self._contacts = dict(res.payload or {})
        except Exception:  # noqa: BLE001
            pass
        types = {1: "compagnons", 2: "repeteurs", 3: "salons", 4: "capteurs"}
        cpt = {v: 0 for v in types.values()}
        for c in self._contacts.values():
            k = types.get(c.get("type"))
            if k:
                cpt[k] += 1
        r["contacts"] = {**cpt, "total": len(self._contacts)}
        r["canaux"] = self.canaux
        # télémétrie Cayenne LPP → liste lisible
        tel = r.get("telemetrie")
        if isinstance(tel, dict):
            tel = tel.get("lpp") or tel.get("data") or []
        r["telemetrie"] = [{"canal": x.get("channel"), "type": x.get("type"), "valeur": x.get("value")}
                           for x in (tel or []) if isinstance(x, dict)]
        for k in ("identite",):
            if isinstance(r.get(k), dict):
                r[k] = {kk: vv for kk, vv in r[k].items() if not isinstance(vv, (bytes, bytearray))}
        if isinstance(r.get("batterie"), dict) and r["batterie"].get("level"):
            r["batterie"]["corrigee"] = self._corr_batterie(int(r["batterie"]["level"]))
        r["diagnostic"] = self._diagnostic_companion(r)
        if isinstance(r.get("batterie"), dict) and r["batterie"].get("level"):
            brut = int(r["batterie"]["level"])
            mv = self._corr_batterie(brut)
            self.batterie = {"mv": mv, "brut": brut, "pct": self._pct_batterie(mv), "date": now_iso()}
        self.analyse_companion = r
        self.store._bump()
        return {"ok": True, "analyse": r}

    def _diagnostic_companion(self, r: dict) -> list[dict]:
        """Points à vérifier : (niveau, texte). niveau = ok / attention / critique."""
        out = []
        idt, app, bat = r.get("identite") or {}, r.get("appareil") or {}, r.get("batterie") or {}
        def ajoute(niv, txt):
            out.append({"niveau": niv, "texte": txt})
        if idt:
            ecarts = [k for k, v in self.RESEAU.items() if idt.get(k) is not None and abs(float(idt[k]) - v) > 0.01]
            if ecarts:
                ajoute("critique", "Paramètres radio différents du réseau ADRASEC 06 (869,618 MHz · BW 62,5 kHz · SF 8 · CR 4/8) : "
                       f"{idt.get('radio_freq')} MHz · BW {idt.get('radio_bw')} kHz · SF {idt.get('radio_sf')} · CR 4/{idt.get('radio_cr')}.")
            else:
                ajoute("ok", "Paramètres radio conformes au réseau ADRASEC 06.")
            if idt.get("tx_power") is not None and idt.get("max_tx_power") and idt["tx_power"] < idt["max_tx_power"] - 3:
                ajoute("attention", f"Puissance d'émission {idt['tx_power']} dBm (maximum de la carte {idt['max_tx_power']} dBm).")
            if not idt.get("telemetry_mode_loc"):
                ajoute("attention", "La position n'est pas partagée en télémétrie : les autres ne verront pas votre position.")
            if not (abs(idt.get("adv_lat") or 0) > 0.01):
                ajoute("attention", "Aucune position enregistrée dans le nœud (annonces sans coordonnées).")
            if idt.get("manual_add_contacts"):
                ajoute("attention", "Ajout manuel des contacts activé : les nouveaux répéteurs entendus ne sont pas ajoutés automatiquement.")
        if bat.get("level"):
            mv = bat.get("corrigee") or bat["level"]
            v = mv / 1000
            seuil = float(self.store.get_config("seuil_batterie", 3.5) or 3.5)
            corr = "" if mv == bat["level"] else f" après correction de {(mv - bat['level']) / 1000:+.2f} V"
            ajoute("critique" if v < seuil else "ok",
                   f"Batterie {v:.2f} V, {self._pct_batterie(mv)} % (même calcul que l'écran du nœud){corr}.".replace(".", ",", 2 if corr else 1))
            tel_v = next((t.get("valeur") for t in r.get("telemetrie") or [] if t.get("type") == "voltage" and isinstance(t.get("valeur"), (int, float))), None)
            if tel_v and abs(tel_v - bat["level"] / 1000) > 0.1:
                fr = lambda x: f"{x:.2f}".replace(".", ",")
                ajoute("attention", f"La télémétrie indique {fr(tel_v)} V alors que la mesure batterie donne {fr(bat['level'] / 1000)} V : "
                       "mesurez la batterie et réglez la correction dans Réglages si besoin.")
        if bat.get("total_kb"):
            pct = 100 * (bat.get("used_kb") or 0) / bat["total_kb"]
            if pct > 85:
                ajoute("attention", f"Mémoire du nœud occupée à {pct:.0f} % : supprimez des contacts inutiles.")
        if r.get("heureCorrigee"):
            e = r["heureCorrigee"]
            ajoute("attention", f"Horloge du nœud {'en avance' if e > 0 else 'en retard'} de {abs(e) // 60} min : remise à l'heure du PC.")
        heure = (r.get("heure") or {}).get("time")
        if heure:
            ecart = int(heure - time.time())
            r["heureEcart"] = ecart
            ajoute("attention" if abs(ecart) > 120 else "ok",
                   f"Horloge du nœud {'à l’heure' if abs(ecart) <= 120 else ('en avance' if ecart > 0 else 'en retard') + f' de {abs(ecart) // 60} min'}.")
        mx = app.get("max_contacts")
        tot = (r.get("contacts") or {}).get("total", 0)
        if mx:
            niv = "critique" if tot >= mx else "attention" if tot >= 0.9 * mx else "ok"
            ajoute(niv, f"{tot} contacts sur {mx} possibles.")
        rad = r.get("radio") or {}
        if rad.get("noise_floor") is not None:
            nf = rad["noise_floor"]
            ajoute("attention" if nf > -100 else "ok", f"Bruit de fond {nf} dBm" + (" : élevé, environnement brouillé." if nf > -100 else "."))
        core = r.get("core") or {}
        if core.get("queue_len"):
            ajoute("attention", f"{core['queue_len']} paquet(s) en attente d'émission.")
        return out

    @staticmethod
    def _pct_batterie(mv: int) -> int:
        """Pourcentage calculé comme l'écran des nœuds MeshCore : linéaire de 3,00 V (0 %) à 4,20 V (100 %)."""
        return max(0, min(100, int((int(mv) - 3000) * 100 / 1200)))

    def etalonner_batterie(self, pct_ecran: float) -> dict:
        """Étalonnage : le nœud affiche pct_ecran % → correction = tension correspondant à ce % − tension lue."""
        b = self.batterie
        if not b or not b.get("brut"):
            return {"ok": False, "erreur": "Batterie du nœud companion pas encore lue : connectez-le et attendez une minute."}
        try:
            pct = max(0.0, min(100.0, float(pct_ecran)))
        except (TypeError, ValueError):
            return {"ok": False, "erreur": "Pourcentage invalide."}
        if pct in (0.0, 100.0):
            return {"ok": False, "erreur": "Étalonnez avec un pourcentage entre 1 et 99 % (à 0 ou 100 % l'écran est saturé)."}
        cible = 3000 + pct * 12 + 6  # milieu de la plage qui donne ce pourcentage à l'écran
        corr = round((cible - b["brut"]) / 1000, 3)
        if abs(corr) > 0.5:
            return {"ok": False, "erreur": f"Écart trop grand ({corr:+.2f} V) : vérifiez le pourcentage saisi."}
        self.store.set_config("corr_batt_companion", corr)
        mv = self._corr_batterie(b["brut"])
        self.batterie = {**b, "mv": mv, "pct": self._pct_batterie(mv)}
        self.store._bump()
        return {"ok": True, "correction": corr, "pct": self.batterie["pct"], "mv": mv}

    def _corr_batterie(self, mv: int) -> int:
        """Correction réglable (Réglages) si la tension lue par le nœud companion est décalée."""
        try:
            return int(mv + float(self.store.get_config("corr_batt_companion", 0) or 0) * 1000)
        except (TypeError, ValueError):
            return int(mv)

    async def _maj_batterie(self):
        """Tension de la batterie du nœud companion (commande get_bat)."""
        mc = self.mc
        if mc is None:
            return
        try:
            ev = await mc.commands.get_bat()
        except Exception as e:  # noqa: BLE001
            log.debug("batterie companion : %s", e)
            return
        if ev is None or ev.type == EventType.ERROR:
            return
        lu = int((ev.payload or {}).get("level") or 0)
        if lu <= 0:
            return
        # médiane des 5 dernières lectures : évite les sauts dus aux pointes de consommation (émission radio)
        hist = getattr(self, "_batt_hist", [])
        hist = (hist + [lu])[-5:]
        self._batt_hist = hist
        brut = sorted(hist)[len(hist) // 2]
        mv = self._corr_batterie(brut)
        self.batterie = {"mv": mv, "brut": brut, "pct": self._pct_batterie(mv), "date": now_iso()}
        seuil = float(self.store.get_config("seuil_batterie", 3.5) or 3.5) * 1000
        if mv < seuil and not self._batt_alerte:
            self._batt_alerte = True
            self.alerte("attention", self.local_name or "nœud companion",
                        f"Batterie faible sur le nœud companion {self.local_name} : {mv / 1000:.2f} V.")
        elif mv >= seuil + 100:
            self._batt_alerte = False
        self.store._bump()

    async def _maj_position(self):
        """Position du nœud companion : GPS de sa télémétrie, sinon position enregistrée dans le nœud."""
        mc = self.mc
        if mc is None:
            return
        pos, source = None, ""
        try:
            ev = await mc.commands.get_self_telemetry()
            if ev is not None and ev.type != EventType.ERROR:
                for x in (ev.payload or {}).get("lpp") or []:
                    if isinstance(x, dict) and x.get("type") == "gps" and isinstance(x.get("value"), dict):
                        v = x["value"]
                        lat, lon = v.get("latitude"), v.get("longitude")
                        if lat is not None and lon is not None and (abs(lat) > 0.01 or abs(lon) > 0.01):
                            pos, source = {"lat": float(lat), "lon": float(lon), "alt": v.get("altitude")}, "GPS"
                            break
        except Exception as e:  # noqa: BLE001
            log.debug("télémétrie du nœud companion : %s", e)
        if pos is None:
            try:
                ev = await mc.commands.send_appstart()
                if ev is not None and ev.type != EventType.ERROR:
                    p = ev.payload or {}
                    lat, lon = p.get("adv_lat") or 0, p.get("adv_lon") or 0
                    if abs(lat) > 0.01 or abs(lon) > 0.01:
                        pos, source = {"lat": float(lat), "lon": float(lon), "alt": None}, "position enregistrée"
            except Exception as e:  # noqa: BLE001
                log.debug("position du nœud companion : %s", e)
        if pos:
            pos.update(source=source, date=now_iso(), nom=self.local_name)
            self.position = pos
        elif self.position:
            self.position["perimee"] = True
        self.store._bump()


    # ---------- redémarrage et extinction à distance ----------
    async def _arret(self, node: dict, contact: dict, cmd: str, operateur: str) -> dict:
        """reboot / poweroff : le répéteur n'envoie aucune réponse ; on vérifie ensuite son état."""
        name = node.get("nom", "")
        sent = await self.mc.commands.send_cmd(contact, cmd)
        if sent is None or sent.type == EventType.ERROR:
            return {"ok": False, "erreur": "Le nœud companion n'a pas pu envoyer la commande."}
        if cmd == "poweroff":
            self.store.patch_node(node["id"], {"eteint": now_iso()})
            self.store.add_journal(name, operateur or "AUTO",
                                   f"Extinction à distance de {name} demandée (poweroff). Rallumage uniquement sur place.")
            await asyncio.sleep(20)
            try:
                st = await self.mc.commands.req_status_sync(contact, min_timeout=10)
            except Exception:
                st = None
            if st:
                msg = f"{name} répond encore après la demande d'extinction : commande non prise en compte."
                self.store.patch_node(node["id"], {"eteint": ""})
                self.store.add_journal(name, "AUTO", msg, auto=True)
                self._log(msg)
                return {"ok": False, "erreur": msg}
            msg = f"Extinction de {name} confirmée : il ne répond plus. Surveillance suspendue jusqu'à son rallumage."
            self.store.add_journal(name, "AUTO", msg, auto=True)
            self._log(msg)
            return {"ok": True, "reponse": "extinction confirmée"}
        # reboot : on attend le redémarrage puis on lit le temps de fonctionnement
        self.store.add_journal(name, operateur or "AUTO", f"Redémarrage à distance de {name} demandé.")
        await asyncio.sleep(25)
        st = None
        for _ in range(2):
            try:
                st = await self.mc.commands.req_status_sync(contact, min_timeout=10)
            except Exception:
                st = None
            if st:
                break
            await asyncio.sleep(10)
        if st and st.get("uptime") is not None and st["uptime"] < 300:
            msg = f"Redémarrage de {name} confirmé (en service depuis {st['uptime']} s)."
            self._marquer_vivant(node, "Redémarrage confirmé", {"uptime": st["uptime"]})
            ok = True
        elif st:
            msg = f"{name} répond mais n'a pas redémarré (en service depuis {st.get('uptime')} s)."
            self._marquer_vivant(node, None, {"uptime": st.get("uptime")})
            ok = False
        else:
            msg = f"{name} ne répond pas encore après le redémarrage ; nouvelle vérification au prochain cycle."
            ok = True
        self.store.add_journal(name, "AUTO", msg, auto=True)
        self._log(msg)
        return {"ok": ok, "reponse": msg} if ok else {"ok": False, "erreur": msg}

    def reprendre(self, node_id: str) -> dict:
        n = self.store.get_node(node_id)
        if not n:
            return {"ok": False, "erreur": "Nœud inconnu."}
        self.store.patch_node(node_id, {"eteint": "", "echecs": 0})
        self.store.add_journal(n.get("nom", ""), self.store.get_config("operateur", "") or "AUTO",
                               f"{n.get('nom')} rallumé : reprise de la surveillance.")
        self.poll_now()
        return {"ok": True}
