# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Linux x86-64 one-file build; paths are rooted at this spec, not the CWD.

import os
import sys

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(SPECPATH)
sys.path.insert(0, ROOT)
ICON_DIR = os.path.join(ROOT, 'mountfile', 'data')
ICON_FILE = os.path.join(ICON_DIR, 'icon.ico' if sys.platform.startswith('win') else 'icon.png')

a = Analysis(
    [os.path.join(ROOT, 'main.py')],
    pathex=[ROOT],
    binaries=[],
    datas=collect_data_files('mountfile'),
    hiddenimports=['mountfile_dfir_tools'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MountFile-linux-x86-64',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_FILE,
)
