"""Entry point for the bundled esptool console executable.

PyInstaller uses this script to build a standalone ``esptool`` binary
that sits next to the main SoftEdIBO executable.  The main app invokes
it via QProcess when flashing firmware in frozen (packaged) mode.
"""

import sys

try:
    # esptool >= 4.x
    import esptool
    sys.exit(esptool.main())
except AttributeError:
    # esptool 3.x
    # Private symbol that only exists in esptool 3.x, so the stubs for the
    # installed (4.x) esptool don't know it.
    from esptool.__main__ import _main  # pyright: ignore[reportAttributeAccessIssue]
    sys.exit(_main())
