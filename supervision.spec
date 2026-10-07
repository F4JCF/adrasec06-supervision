# -*- mode: python ; coding: utf-8 -*-
# Fichier de compilation PyInstaller : un seul .exe fenêtré.
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["run.py"],
    pathex=[],
    datas=[
        ("supervision/ui", "ui"),
        ("supervision/seed.json", "."),
    ],
    hiddenimports=(collect_submodules("meshcore") + collect_submodules("bleak")
                   + collect_submodules("winrt") + ["serial.tools.list_ports"]),
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
