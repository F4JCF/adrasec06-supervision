# -*- mode: python ; coding: utf-8 -*-
# Fichier de compilation PyInstaller : un seul .exe fenêtré.
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

a = Analysis(
    ["run.py"],
    pathex=[],
    datas=[
        ("supervision/ui", "ui"),
        ("supervision/seed.json", "."),
        ("supervision/icone.png", "."),
    ] + collect_data_files("reportlab"),
    hiddenimports=(collect_submodules("meshcore") + collect_submodules("bleak")
                   + collect_submodules("winrt") + collect_submodules("pystray")
                   + collect_submodules("reportlab") + collect_submodules("openpyxl")
                   + collect_submodules("aprslib") + ["serial.tools.list_ports", "PIL.Image"]),
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="Supervision-ADRASEC06",
    console=False,
    icon="assets/icone.ico",
    upx=False,
)
