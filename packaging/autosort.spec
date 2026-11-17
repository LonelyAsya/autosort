# -*- mode: python ; coding: utf-8 -*-
# Сборка: pyinstaller packaging/autosort.spec --noconfirm  →  dist/Autosort/Autosort.exe
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = os.path.dirname(SPECPATH)
FFMPEG = os.path.join(ROOT, "build_ffmpeg")

datas = collect_data_files("autosort")
binaries = []
hiddenimports = []
for package in ("onnxruntime", "pillow_heif", "send2trash"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

# ffmpeg кладётся рядом в папку ffmpeg/, если его скачали перед сборкой
for tool in ("ffmpeg.exe", "ffprobe.exe"):
    if os.path.exists(os.path.join(FFMPEG, tool)):
        binaries.append((os.path.join(FFMPEG, tool), "ffmpeg"))

a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "matplotlib", "pandas", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Autosort",
    icon=os.path.join(SPECPATH, "autosort.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Autosort", upx=False)
