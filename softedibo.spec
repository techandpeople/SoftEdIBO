# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for SoftEdIBO
#
# Build with:
#   pip install pyinstaller
#   pyinstaller softedibo.spec
#
# Output: dist/SoftEdIBO/
#   SoftEdIBO           — main GUI (no console)
#   esptool             — standalone flash tool (console)
#   _internal/          — bundled Python libs + assets
#     config/
#       settings.yaml
#     firmware/
#       gateway/firmware.bin
#       air_chamber_node/firmware.bin

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# ---------------------------------------------------------------------------
# Hidden imports shared by both Analysis calls
# ---------------------------------------------------------------------------
COMMON_EXCLUDES = [
    "tkinter",
    "matplotlib",
    "numpy",
    "scipy",
    "pandas",
    "PIL",
    "IPython",
    "jupyter",
    # SQLAlchemy bundles hooks for every DB backend; exclude the ones we don't ship
    "pysqlite2",   # legacy sqlite2 binding (we use the built-in sqlite3)
    "MySQLdb",     # MySQL backend
    "psycopg2",    # PostgreSQL backend (not installed in the frozen bundle)
]

# ---------------------------------------------------------------------------
# 1. Main application
# ---------------------------------------------------------------------------
main_a = Analysis(
    ["scripts/run.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("config/", "config/"),
        ("firmware/gateway/firmware.bin", "firmware/gateway"),
        ("firmware/air_chamber_node/firmware.bin", "firmware/air_chamber_node"),
    ],
    hiddenimports=[
        *collect_submodules("src"),
        "sqlalchemy.dialects.sqlite",
        "serial.tools.list_ports",
        "PySide6.QtSvg",
        "PySide6.QtXml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=COMMON_EXCLUDES,
    cipher=block_cipher,
    noarchive=False,
)

main_pyz = PYZ(main_a.pure, main_a.zipped_data, cipher=block_cipher)

main_exe = EXE(
    main_pyz,
    main_a.scripts,
    [],
    exclude_binaries=True,
    name="SoftEdIBO",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,      # GUI app — no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---------------------------------------------------------------------------
# 2. Standalone esptool console executable
#    The main app invokes this via QProcess when flashing firmware in
#    frozen mode (see setup_wizard._esptool_cmd).
# ---------------------------------------------------------------------------
esptool_a = Analysis(
    ["_esptool_main.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[
        "esptool",
        "esptool.targets",
        "esptool.targets.esp32",
        "esptool.loader",
        "esptool.cmds",
        "esptool.util",
        "serial.tools.list_ports",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=COMMON_EXCLUDES,
    cipher=block_cipher,
    noarchive=False,
)

esptool_pyz = PYZ(esptool_a.pure, esptool_a.zipped_data, cipher=block_cipher)

esptool_exe = EXE(
    esptool_pyz,
    esptool_a.scripts,
    [],
    exclude_binaries=True,
    name="esptool",
    debug=False,
    strip=False,
    upx=True,
    console=True,       # CLI tool — keep the terminal
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---------------------------------------------------------------------------
# 3. Collect everything into dist/SoftEdIBO/
# ---------------------------------------------------------------------------
coll = COLLECT(
    main_exe,
    main_a.binaries,
    main_a.zipfiles,
    main_a.datas,
    esptool_exe,
    esptool_a.binaries,
    esptool_a.zipfiles,
    esptool_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SoftEdIBO",
)
