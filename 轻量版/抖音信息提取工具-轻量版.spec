# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


light_dir = Path(SPECPATH).resolve()
project_dir = light_dir.parent

a = Analysis(
    [str(light_dir / "light_app.py")],
    pathex=[str(light_dir), str(project_dir)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "app",
        "account_monitor",
        "exporter",
        "storage",
        "PIL",
        "cv2",
        "numpy",
        "onnxruntime",
        "rapidocr_onnxruntime",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="抖音信息提取工具-轻量版",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="抖音信息提取工具-轻量版",
)
