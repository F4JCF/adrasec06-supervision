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
import logging
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
        self._eq_task = None
        self.couverture_en_cours = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="meshcore", daemon=True)
        self.thread.start()
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
            "port": self.port,
            "mode": self.mode,
            "noeudLocal": self.local_name,
            "radio": self.radio if self.mc is not None else {},
            "occupe": self.busy,
            "dernierCycle": self.last_cycle,
            "prochainCycle": self.next_cycle,
            "message": self.message,
            "intervalle": self.interval(),
            "decouverte": bool(self.store.get_config("decouverte", True)),
            "evenements": list(self.events)[:60],
            "canaux": self.canaux if self.mc is not None else [],
            "equipes": self.equipes,
            "position": self.position,
            "couvertureEnCours": self.couverture_en_cours,
            "contactsMessagerie": self.contacts_messagerie() if self.mc is not None else [],
        }

    def interval(self) -> int:
        return int(self.store.get_config("intervalle_min", 15) or 15)

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

    def set_interval(self, minutes: int):
        self.store.set_config("intervalle_min", max(1, int(minutes)))
        if self.mc is not None:
            self.poll_now()

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
        try:
            mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
            mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)
        except Exception as e:  # noqa: BLE001
            log.warning("Abonnement aux messages impossible : %s", e)
        try:
            await mc.start_auto_message_fetching()
        except Exception:
            pass
        await self._load_channels()
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
            wait = self.interval() * 60
            self.next_cycle = time.time() + wait
            self.store._bump()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    # ---------- un cycle d'interrogation ----------
    async def _cycle(self):
        mc = self.mc
        while self.couverture_en_cours:
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
                   if n.get("proprio") != "externe" and n.get("statut") not in ("prevu", "supprime")]
        for i, node in enumerate(targets, 1):
            if self.mc is None:
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
            if node is None:
                self.store.save_node({
                    "nom": name, "proprio": "externe", "statut": "en-ligne", "role": ROLE_BY_TYPE[t],
                    "lat": round(lat, 5) if has_pos else "", "lon": round(lon, 5) if has_pos else "",
                    "voisins": [], "decouvert": True, "dernierAdvert": last_seen,
                    "cle": c.get("public_key", "")[:12],
                }, bump=False)
                added += 1
            else:
                fields = {"dernierAdvert": last_seen, "cle": c.get("public_key", "")[:12], "chemin": self._chemin(c)}
                if has_pos and (node.get("lat") in ("", None) or node.get("lon") in ("", None)):
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

        status = None
        try:
            status = await mc.commands.req_status_sync(contact, min_timeout=10)
        except Exception:
            status = None

        if not status:
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
        for i in range(8):
            try:
                ev = await self.mc.commands.get_channel(i)
            except Exception:
                break
            if ev is None or ev.type == EventType.ERROR:
                break
            name = ((ev.payload or {}).get("channel_name") or "").strip("\x00 ")
            if name:
                out.append({"index": i, "nom": name})
        self.canaux = out or [{"index": 0, "nom": "Public"}]
        self.store._bump()

    def contacts_messagerie(self) -> list[dict]:
        out = [{"cle": c.get("public_key", ""), "nom": c.get("adv_name", "")}
               for c in self._contacts.values() if c.get("type") == 1 and c.get("adv_name")]
        out.sort(key=lambda x: x["nom"].lower())
        return out

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
        self.store.add_message({"sens": "recu", "canal": p.get("channel_idx"), "canal_nom": self._canal_nom(p.get("channel_idx")),
                                "auteur": auteur, "texte": corps, "snr": p.get("SNR"),
                                "sauts": p.get("path_len") if p.get("path_len") not in (255, None) else None})

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
            ev = await self.mc.commands.send_msg(c, texte)
            ok = ev is not None and ev.type != EventType.ERROR
            self.store.add_message({"sens": "emis", "contact": c.get("adv_name"), "contact_cle": contact_cle[:12],
                                    "auteur": moi, "texte": texte, "statut": "envoyé" if ok else "échec"})
        else:
            idx = int(canal or 0)
            ev = await self.mc.commands.send_chan_msg(idx, texte)
            ok = ev is not None and ev.type != EventType.ERROR
            self.store.add_message({"sens": "emis", "canal": idx, "canal_nom": self._canal_nom(idx), "auteur": moi,
                                    "texte": texte, "statut": "envoyé" if ok else "échec"})
        return {"ok": ok} if ok else {"ok": False, "erreur": "Le nœud companion a refusé l'envoi."}

    # ---------- commandes à distance ----------
    COMMANDES = {
        "advert": "advert",
        "reboot": "reboot",
        "powersaving on": "powersaving on",
        "powersaving off": "powersaving off",
        "start ota": "start ota",
        "ver": "ver",
        "clock sync": "clock sync",
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
        if self.busy:
            return {"ok": False, "erreur": "Une interrogation est en cours, réessayez dans un instant."}
        try:
            return self._submit(self._commande(node, cmd, operateur), timeout=90)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erreur": f"Commande impossible : {e}"}

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
        reponse = await self._cli(contact, cmd, timeout=25)
        texte = f"Commande « {cmd} » envoyée à {name}" + (f" — réponse : {reponse.strip()}" if reponse else " — pas de réponse")
        self.store.add_journal(name, operateur or "AUTO", texte)
        if reponse and cmd.startswith("set tx"):
            self.store.patch_node(node["id"], {"tx": int(cmd.split()[-1])})
        if cmd == "start ota":
            self.store.patch_node(node["id"], {"otaDebut": now_iso(), "otaFirmwareAvant": node.get("firmware", ""),
                                               "otaReponse": (reponse or "").strip()})
        if cmd == "ver" and reponse:
            m = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", reponse)
            if m:
                self._confirmer_ota(self.store.get_node(node["id"]) or node, "v" + m.group(1), operateur)
        self._log(texte)
        return {"ok": True, "reponse": reponse or ""}


    # ---------- équipes (companions qui partagent leur position) ----------
    def _update_equipes(self):
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
        if self.busy or self.couverture_en_cours:
            return {"ok": False, "erreur": "Une interrogation est en cours, réessayez dans un instant."}
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return {"ok": False, "erreur": "Position invalide : indiquez la latitude et la longitude du point de mesure."}
        try:
            return self._submit(self._couverture(lat, lon, lieu), timeout=900)
        except Exception as e:  # noqa: BLE001
            self.couverture_en_cours = False
            return {"ok": False, "erreur": f"Test impossible : {e}"}

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
