from pathlib import Path

from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT


ROOT = Path(SPECPATH)
SOURCE = str(ROOT / "server.py")

analysis = Analysis(
    [SOURCE],
    pathex=[str(ROOT.parent)],
    binaries=[],
    datas=[],
    hiddenimports=["PySide6.QtMultimedia", "PySide6.QtNetwork"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="TtsServerAdmin",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="TtsServerAdmin",
)
