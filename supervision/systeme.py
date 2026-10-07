"""Intégration Windows : notifications, icône de la zone de notification, démarrage automatique.

Tout est protégé : sur un autre système, ou si une fonction Windows est indisponible,
le logiciel continue de fonctionner sans elle.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading

log = logging.getLogger("supervision")
IS_WIN = sys.platform.startswith("win")
APP_ID = "ADRASEC06.Supervision"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Supervision ADRASEC 06"


# ---------- notifications ----------
def _ps_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")


def notifier(titre: str, texte: str):
    """Affiche une notification Windows (centre de notifications), sans bloquer."""
    if not IS_WIN:
        log.info("Notification : %s — %s", titre, texte)
        return

    def run():
        script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{_ps_escape(titre)}</text><text>{_ps_escape(texte)}</text></binding></visual><audio src="ms-winsoundevent:Notification.Default"/></toast>')
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($toast)
"""
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            log.warning("Notification Windows impossible : %s", e)

    threading.Thread(target=run, daemon=True).start()


# ---------- démarrage automatique ----------
def _commande_lancement() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --reduit'
    return f'"{sys.executable}" "{sys.argv[0]}" --reduit'


def demarrage_auto_actif() -> bool:
    if not IS_WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def regler_demarrage_auto(actif: bool) -> bool:
    if not IS_WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if actif:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _commande_lancement())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        log.warning("Démarrage automatique : %s", e)
        return False


# ---------- icône de la zone de notification ----------
class IconeZone:
    """Icône près de l'horloge : Afficher, Interroger maintenant, Quitter."""

    def __init__(self, image_path, afficher, interroger, quitter):
        self.icon = None
        try:
            import pystray
            from PIL import Image
            img = Image.open(image_path)
            menu = pystray.Menu(
                pystray.MenuItem("Afficher la supervision", lambda: afficher(), default=True),
                pystray.MenuItem("Interroger maintenant", lambda: interroger()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quitter", lambda: quitter()),
            )
            self.icon = pystray.Icon("supervision-adrasec06", img, "Supervision MeshCore ADRASEC 06", menu)
            self.icon.run_detached()
        except Exception as e:  # noqa: BLE001
            log.warning("Icône de la zone de notification indisponible : %s", e)
            self.icon = None

    @property
    def active(self) -> bool:
        return self.icon is not None

    def titre(self, texte: str):
        if self.icon is not None:
            try:
                self.icon.title = texte[:120]
            except Exception:
                pass

    def arreter(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
