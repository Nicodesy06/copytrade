# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec di M4 CopyDesk. Uso: pyinstaller packaging/m4desk.spec --noconfirm
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
VERSION = os.environ.get("M4DESK_VERSION", "0.1.0")
ICNS = ROOT / "build" / "icon" / "M4CopyDesk.icns"

hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("m4desk")
    + collect_submodules("sqlalchemy.dialects.sqlite")
    + ["websockets.legacy", "websockets.asyncio.client", "pydantic_settings"]
)
if sys.platform == "darwin":
    hidden += ["rumps"]

a = Analysis(
    [str(ROOT / "packaging" / "m4desk_app.py")],
    pathex=[str(ROOT / "src")],
    datas=collect_data_files("m4desk") + collect_data_files("certifi"),
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "ruff", "IPython", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="M4 CopyDesk",
    console=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="M4 CopyDesk")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="M4 CopyDesk.app",
        icon=str(ICNS) if ICNS.exists() else None,
        bundle_identifier="com.m4copydesk.app",
        version=VERSION,
        info_plist={
            "CFBundleName": "M4 CopyDesk",
            "CFBundleDisplayName": "M4 CopyDesk",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "13.0",
            "LSUIElement": True,  # app della barra dei menu (nessuna icona nel Dock)
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.finance",
            "NSHumanReadableCopyright": "Solo ricerca e simulazione PAPER. Nessuna transazione reale.",
        },
    )
