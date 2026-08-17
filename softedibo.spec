# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for SoftEdIBO
#
# Build with:
#   pip install pyinstaller
#   pyinstaller softedibo.spec
#
# Output: dist/SoftEdIBO/
#   SoftEdIBO           - main GUI (no console)
#   esptool             - standalone flash tool (console)
#   _internal/          - bundled Python libs + assets
#     config/
#       settings.yaml
#     firmware/                                      (see scripts/build-firmware.sh)
#       gateway/firmware-s3.bin                      (XIAO ESP32-S3, ESP-IDF)
#       node_actuator/firmware-direct-{release,debug}.bin
#       node_actuator/firmware-direct-rgbw-{release,debug}.bin
#       node_actuator/firmware-multiplexed-{release,debug}.bin
#       node_actuator/firmware-multiplexed-rgbw-{release,debug}.bin
#       node_magnet_sensor/firmware-release.bin      (MLX90393 touch board)
#       thymio_rcp/firmware.bin                      (XIAO ESP32-C6 RCP, WiFi-OTA app image)

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# ---------------------------------------------------------------------------
# Excludes
# ---------------------------------------------------------------------------
# Shared by both Analysis calls - pure bloat neither executable ever needs.
COMMON_EXCLUDES = [
    "tkinter",
    "matplotlib",
    "PIL",
    "IPython",
    "jupyter",
    # SQLAlchemy bundles hooks for every DB backend; exclude the ones we don't ship
    "pysqlite2",   # legacy sqlite2 binding (we use the built-in sqlite3)
    "MySQLdb",     # MySQL backend
    "psycopg2",    # PostgreSQL backend (not installed in the frozen bundle)
]

# The standalone esptool executable does no ML - keep it lean by stripping the
# numeric stack there. The main app DOES use it (touch-gesture classifier:
# numpy / scipy / scikit-learn / joblib), so those must NOT be excluded from it.
ESPTOOL_EXCLUDES = COMMON_EXCLUDES + ["numpy", "scipy", "pandas"]

# ---------------------------------------------------------------------------
# 1. Main application
# ---------------------------------------------------------------------------
main_a = Analysis(
    ["scripts/run.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("config/", "config/"),
        # App icon: the .ico is baked into the exe resource below (Windows file
        # / title-bar icon); the .png is what Qt loads at runtime for the
        # window + taskbar icon (see src/gui/app_icon.py).
        ("softedibo.png", "."),
        # Block editor assets (Tools => Behaviour Editor...). HTML + any vendored
        # Blockly copy; loaded lazily, only when the editor is opened.
        ("src/gui/blockly/", "src/gui/blockly/"),
        ("firmware/gateway/firmware-s3.bin",                             "firmware/gateway"),
        ("firmware/node_actuator/firmware-direct-release.bin",           "firmware/node_actuator"),
        ("firmware/node_actuator/firmware-direct-debug.bin",             "firmware/node_actuator"),
        ("firmware/node_actuator/firmware-direct-rgbw-release.bin",      "firmware/node_actuator"),
        ("firmware/node_actuator/firmware-direct-rgbw-debug.bin",        "firmware/node_actuator"),
        ("firmware/node_actuator/firmware-multiplexed-release.bin",      "firmware/node_actuator"),
        ("firmware/node_actuator/firmware-multiplexed-debug.bin",        "firmware/node_actuator"),
        ("firmware/node_actuator/firmware-multiplexed-rgbw-release.bin", "firmware/node_actuator"),
        ("firmware/node_actuator/firmware-multiplexed-rgbw-debug.bin",   "firmware/node_actuator"),
        ("firmware/node_magnet_sensor/firmware-release.bin",             "firmware/node_magnet_sensor"),
        ("firmware/thymio_rcp/firmware.bin",                             "firmware/thymio_rcp"),
    ],
    hiddenimports=[
        *collect_submodules("src"),
        "sqlalchemy.dialects.sqlite",
        "serial.tools.list_ports",
        "PySide6.QtSvg",
        "PySide6.QtXml",
        # Block editor web view (Tools => Behaviour Editor...). PyInstaller's
        # PySide6 hook bundles the QtWebEngine process when these are imported.
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineCore",
        # Touch-gesture ML stack (lazily imported in src/ml/training.py).
        # scikit-learn pulls these in but the lazy imports can dodge static
        # analysis, so name them explicitly.
        *collect_submodules("sklearn"),
        "joblib",
        "numpy",
        "scipy",
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
    console=False,      # GUI app - no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows exe resource icon (Explorer, taskbar pin, shortcuts). Must be a
    # real multi-size .ico - handing PyInstaller a .png makes it try to convert
    # via Pillow, which is not a build dependency. Regenerate from
    # softedibo.png with scripts/make_icon.py after changing the artwork.
    icon="softedibo.ico",
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
    datas=[
        *collect_data_files("esptool"),
    ],
    hiddenimports=[
        *collect_submodules("esptool"),
        "serial.tools.list_ports",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=ESPTOOL_EXCLUDES,
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
    console=True,       # CLI tool - keep the terminal
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
