# -*- mode: python ; coding: utf-8 -*-
import json
import os
import shutil

import playwright
from PyInstaller.utils.hooks import collect_all

datas = [('wu.ico', '.')]
binaries = []
hiddenimports = []
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tkcalendar')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['app.pyw'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='WUnder CSV',
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
    icon=['wu.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WUnder CSV',
)

# Playwright's Chromium isn't part of the pip package - copy the revisions this
# playwright version expects from the local cache into dist/<app>/ms-playwright,
# where app.pyw points PLAYWRIGHT_BROWSERS_PATH when frozen. Both builds are
# needed: headless launches use the headless shell, but some paths fall back
# to the full build.
browsers_json = os.path.join(os.path.dirname(playwright.__file__), 'driver', 'package', 'browsers.json')
with open(browsers_json, encoding='utf-8') as f:
    revisions = {b['name']: b['revision'] for b in json.load(f)['browsers']}
cache = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or os.path.join(os.environ['LOCALAPPDATA'], 'ms-playwright')
dest = os.path.join(DISTPATH, 'WUnder CSV', 'ms-playwright')
for name, folder in [('chromium', 'chromium'), ('chromium-headless-shell', 'chromium_headless_shell')]:
    src = os.path.join(cache, f'{folder}-{revisions[name]}')
    if not os.path.isdir(src):
        raise SystemExit(f'Missing {src} - run: playwright install chromium')
    shutil.copytree(src, os.path.join(dest, os.path.basename(src)), dirs_exist_ok=True)
