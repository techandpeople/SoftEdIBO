"""Entry point for the bundled esptool console executable.

PyInstaller uses this script to build a standalone ``esptool`` binary
that sits next to the main SoftEdIBO executable.  The main app invokes
it via QProcess when flashing firmware in frozen (packaged) mode.
"""

import sys

import esptool

sys.exit(esptool.main())
