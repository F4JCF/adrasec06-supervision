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
# Secteur retenu pour la découverte automatique : autour de Mougins
CENTRE = (43.60, 7.00)
RAYON_KM = 80
PREFIXES_LOCAUX = ("FR06", "06")


def _distance_km(lat1, lon1, lat2, lon2) -> float:
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def dans_le_secteur(name: str, lat, lon) -> bool:
    """Vrai si le répéteur appartient au secteur : nom en FR06/06, ou position à moins de RAYON_KM."""
    if (name or "").strip().upper().startswith(PREFIXES_LOCAUX):
        return True
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if abs(lat) < 0.01 and abs(lon) < 0.01:
        return False
    return _distance_km(lat, lon, *CENTRE) <= RAYON_KM
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
    def __init__(self, store: Store):
        self.store = store
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
        except Exception:
            pass
        try:
            await mc.start_auto_message_fetching()
        except Exception:
            pass
        self.store.set_config("dernier_mode", mode)
        self.store.set_config("dernier_ble" if ble else "dernier_port", cible)
        self.message = f"Connecté à {self.local_name or 'nœud'} ({self.port})"
        self._log(f"Connexion au nœud {self.local_name or ''} par {'Bluetooth' if ble else 'USB sur ' + cible}")
        self._wake = asyncio.Event()
        self._task = asyncio.ensure_future(self._cycle_loop())
        return {"ok": True}

    async def _disconnect(self):
        if self._task:
            self._task.cancel()
            self._task = None
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
        self.busy = True
        self.message = "Lecture des contacts du nœud companion…"
        self.store._bump()

        res = await mc.commands.get_contacts()
        if res is None or res.type == EventType.ERROR:
            raise RuntimeError("le nœud companion ne renvoie pas sa liste de contacts")
        self._contacts = dict(res.payload or {})

        if self.store.get_config("decouverte", True):
            self._discover()

        targets = [n for n in self.store.list_nodes()
                   if n.get("proprio") != "externe" and n.get("statut") != "prevu"]
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
        """Ajoute ou complète les répéteurs entendus, limités au secteur (FR06 ou < 80 km de Mougins).

        Les nœuds ajoutés automatiquement qui sont hors secteur sont retirés."""
        added = removed = 0
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
                if not dans_le_secteur(name, lat, lon):
                    continue
                self.store.save_node({
                    "nom": name, "proprio": "externe", "statut": "en-ligne", "role": ROLE_BY_TYPE[t],
                    "lat": round(lat, 5) if has_pos else "", "lon": round(lon, 5) if has_pos else "",
                    "voisins": [], "decouvert": True, "dernierAdvert": last_seen,
                    "cle": c.get("public_key", "")[:12],
                }, bump=False)
                added += 1
            else:
                fields = {"dernierAdvert": last_seen, "cle": c.get("public_key", "")[:12]}
                if has_pos and (node.get("lat") in ("", None) or node.get("lon") in ("", None)):
                    fields.update(lat=round(lat, 5), lon=round(lon, 5))
                node.update(fields)
                self.store.save_node(node, bump=False)
        # nettoyage des nœuds découverts automatiquement hors secteur
        for n in self.store.list_nodes():
            if n.get("decouvert") and n.get("proprio") == "externe" and not dans_le_secteur(n.get("nom"), n.get("lat"), n.get("lon")):
                self.store.delete_node(n["id"])
                removed += 1
        if added:
            self._log(f"{added} nouveau(x) répéteur(s) du secteur découvert(s) via les annonces")
        if removed:
            self._log(f"{removed} répéteur(s) hors secteur retiré(s) de la liste")
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
            await self._mark_failure(node)
            return

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
            tx = await self._cli(contact, "get tx")
            if tx:
                m = re.search(r"(-?\d+)", tx)
                if m:
                    fields["tx"] = int(m.group(1))

        old = node.get("statut")
        if old in ("degrade", "hors-ligne"):
            fields["statut"] = "en-ligne"
            self.store.add_journal(name, "AUTO", f"{name} répond de nouveau (était {old}).", auto=True)
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
        self.store.patch_node(node["id"], fields)
